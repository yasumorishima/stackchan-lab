import asyncio
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server_tools as t  # noqa: E402


async def main():
    async with aiohttp.ClientSession() as s:
        print(await t.get_cheer_song(s, {"player": "京田"}))


asyncio.run(main())
