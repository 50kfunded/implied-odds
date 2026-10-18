"""I rebuild the retained market report and a separate synthetic illustration offline."""

import logging
import sqlite3

from implied_calibration.reproduction import main


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError,ValueError,sqlite3.Error) as exc:
        logging.getLogger(__name__).exception('Reproduction stopped: %s',exc)
        raise SystemExit(1)
