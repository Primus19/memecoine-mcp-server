from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.economic_calendar import SNAPSHOT_PATH, scan_official_composite


def main() -> None:
    os.environ["ECONOMIC_CALENDAR_LOOKAHEAD_DAYS"] = "31"
    result = scan_official_composite()
    result["snapshot_generated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = Path(str(SNAPSHOT_PATH) + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(SNAPSHOT_PATH)
    print(f"wrote {SNAPSHOT_PATH} with {len(result['events'])} events")


if __name__ == "__main__":
    main()
