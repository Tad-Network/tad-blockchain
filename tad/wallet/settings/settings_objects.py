from dataclasses import dataclass

from tad.util.streamable import Streamable, streamable


@dataclass(frozen=True)
@streamable
class BackupInitialized(Streamable):
    """
    Stores user decision regarding import of backup info
    """

    user_initialized: bool  # Stores if user made a selection in UI. (Skip vs Import backup)
    user_skipped: bool  # Stores if user decided to skip import of backup info
    backup_info_imported: bool  # Stores if backup info has been imported
    new_wallet: bool  # Stores if this wallet is newly created / not restored from backup
