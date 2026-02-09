"""Restore filters from backup."""

import asyncio
import logging
from typing import List, Optional

from src.models.backup_models import Backup
from src.models.filter_models import ProtonMailFilter
from src.scraper.protonmail_sync import ProtonMailSync
from src.utils.config import Credentials

logger = logging.getLogger(__name__)


class RestoreEngine:
    """Restore filter state from a backup."""

    def __init__(self, sync: ProtonMailSync):
        self.sync = sync

    async def restore_from_backup(self, backup: Backup, current_filters: List[ProtonMailFilter]) -> dict:
        """Restore filters to match backup state.

        For each filter in the backup:
        - If enabled in backup: enable in ProtonMail
        - If disabled in backup: disable in ProtonMail

        Returns a report dict.
        """
        report = {
            "enabled": [],
            "disabled": [],
            "not_found": [],
            "already_correct": [],
            "errors": [],
        }

        backup_by_name = {f.name: f for f in backup.filters}
        current_by_name = {f.name: f for f in current_filters}

        for name, backup_filter in backup_by_name.items():
            if name not in current_by_name:
                report["not_found"].append(name)
                logger.warning("Filter '%s' not found in current state (may have been deleted)", name)
                continue

            current_filter = current_by_name[name]

            if backup_filter.enabled == current_filter.enabled:
                report["already_correct"].append(name)
                continue

            try:
                if backup_filter.enabled:
                    success = await self.sync.enable_filter(name)
                    if success:
                        report["enabled"].append(name)
                    else:
                        report["errors"].append(f"Failed to enable: {name}")
                else:
                    success = await self.sync.disable_filter(name)
                    if success:
                        report["disabled"].append(name)
                    else:
                        report["errors"].append(f"Failed to disable: {name}")
            except Exception as e:
                report["errors"].append(f"{name}: {e}")
                logger.error("Error restoring filter '%s': %s", name, e)

        logger.info(
            "Restore complete: %d enabled, %d disabled, %d not found, %d already correct, %d errors",
            len(report["enabled"]), len(report["disabled"]),
            len(report["not_found"]), len(report["already_correct"]),
            len(report["errors"]),
        )
        return report
