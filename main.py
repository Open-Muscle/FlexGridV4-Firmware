# main.py - OpenMuscle FlexGrid V4 entry point.
# Runs after boot.py. Any crash here still leaves REPL accessible.

import asyncio
import flexgrid
import logger

logger.info("Booting FlexGrid V4")
asyncio.run(flexgrid.main())
