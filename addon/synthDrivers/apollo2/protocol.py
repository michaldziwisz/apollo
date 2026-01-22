# -*- coding: UTF-8 -*-
from __future__ import annotations

# Serial control bytes / protocol tokens.
CR = b"\r"
NAK = b"\x15"
MUTE = b"\x18"

# Apollo indexing commands (used for Say All / speech cancellation).
#
# Earlier driver iterations found that the "@1?" / "@1+" variant could result in stray "1"
# announcements on some firmware, so we stick to the "@I?" / "@I+" form.
INDEX_QUERY_COMMAND = b"@I?"
INDEX_ENABLE_COMMAND = b"@I+ "
INDEX_MARK_COMMAND = b"@I+ "

