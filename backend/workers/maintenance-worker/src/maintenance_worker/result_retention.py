from __future__ import annotations

import json

from maintenance_worker.jobs import runtime_result_retention


def main() -> None:
    print(json.dumps(runtime_result_retention(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
