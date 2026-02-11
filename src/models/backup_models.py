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


class Backup(BaseModel):
    version: str = "1.0"
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: BackupMetadata = Field(default_factory=BackupMetadata)
    filters: List[ProtonMailFilter] = Field(default_factory=list)
    sieve_script: str = ""
    checksum: str = ""
