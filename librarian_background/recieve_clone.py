"""
The twin of send_clone.py, this file contains the code for recieving a clone
from a remote librarian. We loop through the incoming transfers and check
to see if they have completed.
"""

import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sqlalchemy.orm import Session

from hera_librarian.deletion import DeletionPolicy
from hera_librarian.models.clone import CloneCompleteRequest, CloneCompleteResponse
from librarian_server.database import get_session
from librarian_server.logger import ErrorCategory, ErrorSeverity, log_to_database
from librarian_server.orm import (
    File,
    IncomingTransfer,
    Instance,
    Librarian,
    StoreMetadata,
    TransferStatus,
)

from .task import Task

logger = logging.getLogger("schedule")


class RecieveClone(Task):
    """
    Recieves incoming files from other librarians.
    """

    deletion_policy: DeletionPolicy = DeletionPolicy.DISALLOWED

    def on_call(self):
        with get_session() as session:
            return self.core(session=session)

    def core(self, session: Session):
        """
        Checks for incoming transfers and processes them.
        """

        # Find incoming transfers that are ONGOING
        ongoing_transfers: list[IncomingTransfer] = (
            session.query(IncomingTransfer)
            .filter_by(status=TransferStatus.ONGOING)
            .all()
        )

        all_transfers_succeeded = True

        if len(ongoing_transfers) == 0:
            logger.info("No ongoing transfers to process.")

        for transfer in ongoing_transfers:
            # Check if the transfer has completed

            store: StoreMetadata = transfer.store

            if store is None:
                log_to_database(
                    severity=ErrorSeverity.CRITICAL,
                    category=ErrorCategory.PROGRAMMING,
                    message=(
                        f"Transfer {transfer.id} has no store associated with it. "
                        "Skipping for now, but this should never happen."
                    ),
                    session=session,
                )

                all_transfers_succeeded = False

                continue

            try:
                path_info = store.store_manager.path_info(Path(transfer.staging_path))
            except TypeError:
                log_to_database(
                    severity=ErrorSeverity.ERROR,
                    category=ErrorCategory.DATA_AVAILABILITY,
                    message=(
                        f"Transfer {transfer.id}: cannot get information about staging "
                        f"path: {transfer.staging_path}. Skipping for now."
                    ),
                    session=session,
                )

                all_transfers_succeeded = False

                continue

            # TODO: Make this check more robust? Could have transfer managers provide checks?
            if (
                path_info.md5 == transfer.transfer_checksum
                and path_info.size == transfer.transfer_size
            ):
                # The transfer has completed. Create an instance for this file.
                logger.info(
                    f"Transfer {transfer.id} has completed. Moving file to store and creating instance."
                )

                # Move the file to the store.
                # TODO: Check where that store path is coming from!
                try:
                    store.store_manager.commit(
                        Path(transfer.staging_path), Path(transfer.store_path)
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to move file {transfer.staging_path} to store "
                        f"{store.name} at {transfer.store_path}. Exception: {e}. Skipping for now."
                    )

                    all_transfers_succeeded = False

                    continue

                # Create a new File object
                file = File.new_file(
                    filename=transfer.upload_name,
                    checksum=transfer.transfer_checksum,
                    size=transfer.transfer_size,
                    uploader=transfer.uploader,
                    source=transfer.source,
                )

                # Create an instance for this file.
                instance = Instance.new_instance(
                    path=path_info.path,
                    file=file,
                    store=store,
                    deletion_policy=self.deletion_policy,
                )

                session.add(file)
                session.add(instance)

                # Mark the transfer as completed.
                transfer.status = TransferStatus.COMPLETED
                transfer.end_time = datetime.datetime.utcnow()

                # Commit the changes.
                session.commit()

                # Callback to the source librarian.
                librarian: Optional[Librarian] = (
                    session.query(Librarian).filter_by(name=transfer.source).first()
                )

                if librarian:
                    # Need to call back
                    logger.info(
                        f"Transfer {transfer.id} has completed. Calling back to librarian {librarian.name}."
                    )

                    request = CloneCompleteRequest(
                        source_transfer_id=transfer.id,
                        destination_instance_id=instance.id,
                        store_id=store.id,
                    )

                    try:
                        response: CloneCompleteResponse = librarian.client.post(
                            endpoint="clone/complete",
                            request_model=request,
                            response_model=CloneCompleteResponse,
                        )
                    except Exception as e:
                        log_to_database(
                            severity=ErrorSeverity.ERROR,
                            category=ErrorCategory.LIBRARIAN_NETWORK_AVAILABILITY,
                            message=(
                                f"Failed to call back to librarian {librarian.name} "
                                f"with exception {e}."
                            ),
                            session=session,
                        )
                else:
                    logger.error(
                        f"Transfer {transfer.id} has no source librarian. Cannot callback."
                    )

                # Can now delete the file
                store.store_manager.unstage(Path(transfer.staging_path))
            else:
                logger.info(f"Transfer {transfer.id} has not yet completed. Skipping.")
                continue

        return all_transfers_succeeded
