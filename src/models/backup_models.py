from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator

from src.models.filter_models import ProtonMailFilter, split_legacy_carried_values


class BackupMetadata(BaseModel):
    filter_count: int = 0
    enabled_count: int = 0
    disabled_count: int = 0
    account_email: str = ""
    tool_version: str = "0.1.0"


# 1.0: filters only.
# 1.1: filters carry raw scrape evidence (ProtonMailFilter.raw) and
#      scrape_issues. 1.0 backups still load; see BackupManager checksums.
# 1.2: filters carry is_sieve (Edit opened the Sieve editor, not the wizard).
# 1.3: no new fields. Written only by versions whose parser matches scraped
#      values exactly; earlier ones misread some operators (see
#      STRICT_PARSER_FORMAT_VERSION).
BACKUP_FORMAT_VERSION = "1.3"

# The first format written by a strict parser. Older ProtonFusion matched
# scraped strings by substring and fell back to defaults, so "is not" was
# stored as "is", "does not contain" as "contains", and "begins with" or
# "ends with" as "contains". A backup older than this may hold such
# misread conditions, which nothing in the file can reveal.
STRICT_PARSER_FORMAT_VERSION = "1.3"


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
    # The backup format whose reader produced `filter`: the source backup's
    # version for a scraped filter, BACKUP_FORMAT_VERSION for one rebuilt
    # from Sieve (carry-forward) or scraped live (cleanup). None means
    # unknown: an entry written before this field existed, which is
    # treated as predating the strict parser (see
    # backup_manager.entry_predates_strict_parser).
    source_format: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def split_legacy_carried(cls, data):
        """Read an unstamped carried-forward filter's "|"-joined value as a list.

        An entry without source_format was written before carry-forward
        built values lists, when it stored several keys "|"-joined (see
        split_legacy_carried_values). A stamped entry's values are taken
        as stored: today a single carried key may contain "|".
        """
        if isinstance(data, dict) and data.get("source_format") is None and isinstance(data.get("filter"), dict):
            data = dict(data, filter=split_legacy_carried_values(data["filter"]))
        return data


class Archive(BaseModel):
    version: str = "1.0"
    entries: List[ArchiveEntry] = Field(default_factory=list)
