"""
Consumers for the background task queues. Note that these must run in a separate
thread than the main background tasks to avoid blocking during long-running
synchronous communication. There are two main tasks:

- ConsumeQueue, that takes the queue generated by send_clone and sends off the
  batched transfers.
- CheckConsumedQueue, that looks at completed tasks from ConsumeQueue and
  programatically checks whether their transfers have successfuly 'gone through'.
"""

import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sqlalchemy import select

from hera_librarian.exceptions import LibrarianError
from hera_librarian.transfer import TransferStatus
from librarian_server.database import get_session
from librarian_server.logger import ErrorCategory, ErrorSeverity, log_to_database
from librarian_server.orm.sendqueue import SendQueue
from librarian_server.settings import server_settings

from .task import Task

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class ConsumeQueue(Task):
    """
    A task that consumes the SendQueue, one by one, until it is drained
    or the time is up.
    """

    def core(self, session_maker):
        current_time = datetime.datetime.now(datetime.timezone.utc)
        timeout_after = current_time + (
            self.soft_timeout
            if self.soft_timeout is not None
            else datetime.timedelta(days=100)
        )

        while datetime.datetime.now(datetime.timezone.utc) <= timeout_after:
            # Controlled by retries.
            ret = consume_queue_item(session_maker=session_maker)

            if not ret:
                break

        return

    # pragma: no cover
    def on_call(self):
        self.core(session_maker=get_session)


class CheckConsumedQueue(Task):
    """
    A task that checks on consumed items to see if their transfers
    have completed already.
    """

    complete_status: TransferStatus = TransferStatus.STAGED
    "The status to set the completed items to. Leave this as default if you are doing typical inter-librarian transfers"

    def core(self, session_maker):
        current_time = datetime.datetime.now(datetime.timezone.utc)
        timeout_after = current_time + (
            self.soft_timeout
            if self.soft_timeout is not None
            else datetime.timedelta(days=100)
        )

        check_on_consumed(
            session_maker=session_maker,
            timeout_after=timeout_after,
            complete_status=self.complete_status,
        )
        return

    # pragma: no cover
    def on_call(self):
        self.core(session_maker=get_session)


def check_on_consumed(
    session_maker: Callable[[], "Session"],
    timeout_after: datetime.datetime,
    complete_status: TransferStatus = TransferStatus.STAGED,
) -> bool:
    """
    Check on the 'consumed' SendQueue items. Loop through everything with
    consumed = True, and ask to see if their transfers have gone through.

    There are three possible results:

    1. The transfer is still marked as INTIATED, which means that it is
       still ongoing. It is left as-is.
    2. The transfer is marked as COMPLETED. All downstream OutgoingTransfer
       objects will be updated to complete_status.
    3. The transfer is marked as FAILED. All downstream OutgoingTransfer
       objects will be updated to also have been failed.

    Parameters
    ----------

    session_maker: Callable[[], Session]
        A callable that returns a new session object.
    complete_status: TransferStatus
        The status to mark the transfer as if it is complete. By default, this
        is STAGED. All OutgoingTransfer objects will have their status' updated
        in this case.

    Returns
    -------

    status: bool
        If we return False, then there was nothing to consume. A return value of
        True indicates that we consmed an item.
    """

    with session_maker() as session:
        stmt = select(SendQueue).with_for_update(skip_locked=True)
        stmt = stmt.filter_by(consumed=True).filter_by(completed=False)
        queue_items = session.execute(stmt).scalars().all()

        if len(queue_items) == 0:
            return False

        for queue_item in queue_items:
            if datetime.datetime.now(datetime.timezone.utc) > timeout_after:
                # We are out of time.
                return False

            current_status = queue_item.async_transfer_manager.transfer_status(
                settings=server_settings
            )

            if current_status == TransferStatus.INITIATED:
                continue
            elif current_status == TransferStatus.COMPLETED:
                if complete_status == TransferStatus.STAGED:
                    try:
                        queue_item.update_transfer_status(
                            new_status=complete_status,
                            session=session,
                        )
                    except LibrarianError as e:
                        log_to_database(
                            severity=ErrorSeverity.WARNING,
                            category=ErrorCategory.LIBRARIAN_NETWORK_AVAILABILITY,
                            message=(
                                f"Librarian {queue_item.destination} was not available for "
                                f"contact, returning error {e}. We will try again later."
                            ),
                            session=session,
                        )

                        continue
                    except AttributeError as e:
                        # This is a larger problem; we are missing the associated
                        # librarian in the database. Better ping!
                        log_to_database(
                            severity=ErrorSeverity.CRITICAL,
                            category=ErrorCategory.LIBRARIAN_NETWORK_AVAILABILITY,
                            message=(
                                f"Librarian {queue_item.destination} was not found in "
                                f"the database, returning error {e}. Will try again later "
                                "to complete this transfer, but remedy is suggested."
                            ),
                        )

                        continue
                else:
                    raise ValueError(
                        "No other status than STAGED is supported for checking on consumed"
                    )
            elif current_status == TransferStatus.FAILED:
                for transfer in queue_item.transfers:
                    transfer.fail_transfer(session=session, commit=False)
            else:
                log_to_database(
                    severity=ErrorSeverity.WARNING,
                    category=ErrorCategory.TRANSFER,
                    message=(
                        f"Incompatible return value for transfer status from "
                        f"SendQueue item {queue_item.id} ({current_status})."
                    ),
                    session=session,
                )
                continue

            # If we got down here, we can mark the transfer as consumed.
            queue_item.completed = True
            queue_item.completed_time = datetime.datetime.now(datetime.timezone.utc)

            session.commit()

    return True


def consume_queue_item(session_maker: Callable[[], "Session"]) -> bool:
    """
    Consume the current, oldest, and highest priority item.

    If we return False, then there was nothing to consume. A return value of
    True indicates that we consmed an item.
    """

    with session_maker() as session:
        stmt = select(SendQueue).with_for_update(skip_locked=True)
        stmt = stmt.filter_by(completed=False).filter_by(consumed=False)
        stmt = stmt.order_by(SendQueue.priority.desc(), SendQueue.created_time)
        queue_item = session.execute(stmt).scalar()

        if queue_item is None:
            # Nothing to do!
            return False

        # Otherwise, we are free to consume this item.
        transfer_list = [
            (Path(x.source_path), Path(x.dest_path)) for x in queue_item.transfers
        ]
        # Need to create a copy here in case there is an internal state
        # change. Otherwise SQLAlchemy won't write it back.
        transfer_manager = queue_item.async_transfer_manager.model_copy()
        success = transfer_manager.batch_transfer(
            transfer_list, settings=server_settings
        )

        if success:
            queue_item.consumed = True
            queue_item.consumed_time = datetime.datetime.now(datetime.timezone.utc)

            # Be careful, the internal state of the async transfer manager
            # may have changed. Send it back.
            queue_item.async_transfer_manager = transfer_manager
        else:
            queue_item.retries += 1

            if queue_item.retries > server_settings.max_async_send_retries:
                queue_item.fail(session=session)

        session.commit()

    return True
