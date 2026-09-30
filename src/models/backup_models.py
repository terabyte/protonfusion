from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

from src.models.filter_models import ProtonMailFilter


class BackupMetadata(BaseModel):
    filter_count: int = 0
    enabled_count: int = 0
    disabled_count: int = 0
    account_email: str = ""
    tool_version: str = "0.1.0"


# 1.0: filters only.
# 1.1: filters carry raw scrape evidence (ProtonMailFilter.raw) and
#      scrape_issues. 1.0 backups still load; see BackupManager checksums.
BACKUP_FORMAT_VERSION = "1.1"


class Backup(BaseModel):
    # Default stays "1.0" so a backup.json with no version field (the
    # oldest format) is read as 1.0; BackupManager stamps new backups.
    version: str = "1.0"
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: BackupMetadata = Field(default_factory=BackupMetadata)
    filters: List[ProtonMailFilter] = Field(default_factory=list)
    sieve_script: str = ""
    checksum: str = ""


class ArchiveEntry(BaseModel):
    filter: ProtonMailFilter
    archived_at: str = ""
    source_snapshot: str = ""


class Archive(BaseModel):
    version: str = "1.0"
    entries: List[ArchiveEntry] = Field(default_factory=list)
