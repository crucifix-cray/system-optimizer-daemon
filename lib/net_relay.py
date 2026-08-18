#!/usr/bin/env python3
"""
Local TCP shim: listens on localhost:PORT and relays raw Stratum bytes
through the Railway WSS bridge to the pool.

XMRig ──TCP──► localhost:3334
                    │
                 local_shim.py
                    │
                   WSS
                    │
         chimera-bridge-production.up.railway.app
                    │
              pool.supportxmr.com:3333

Overhead: one extra network hop + WSS framing (~1ms on LAN).
All data is passed as raw binary websocket frames — no parsing.
"""

import asyncio
import logging
import sys
import websockets

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
log = logging.getLogger(__name__)

LISTEN_HOST  = '127.0.0.1'
LISTEN_PORT  = int(sys.argv[1]) if len(sys.argv) > 1 else 3334
BRIDGE_URL   = sys.argv[2] if len(sys.argv) > 2 else 'wss://chimera-bridge-production.up.railway.app'


async def handle(client_reader, client_writer):
    peer = client_writer.get_extra_info('peername')
    log.info(f'[+] miner connected from {peer}')
    log.info(f'[→] opening bridge: {BRIDGE_URL}')

    try:
        async with websockets.connect(
            BRIDGE_URL,
            ping_interval=30,
            ping_timeout=10,
            max_size=None,
            compression=None,   # no compression = lower latency
        ) as ws:
            log.info(f'[✓] bridge connected')

            async def miner_to_bridge():
                """Read raw TCP from XMRig, send as WS binary frames upstream"""
                try:
                    while True:
                        data = await client_reader.read(8192)
                        if not data:
                            break
                        await ws.send(data)
                except Exception as e:
                    log.debug(f'[miner→bridge] {e}')

            async def bridge_to_miner():
                """Receive WS frames from bridge, write raw TCP back to XMRig"""
                try:
                    async for msg in ws:
                        data = msg if isinstance(msg, bytes) else msg.encode()
                        client_writer.write(data)
                        await client_writer.drain()
                except Exception as e:
                    log.debug(f'[bridge→miner] {e}')

            await asyncio.gather(
                miner_to_bridge(),
                bridge_to_miner(),
                return_exceptions=True
            )

    except Exception as e:
        log.error(f'[!] bridge error: {e}')
    finally:
        log.info(f'[-] miner disconnected')
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass


async def main():
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    log.info('=' * 55)
    log.info(f'Local TCP shim')
    log.info(f'Listen : {LISTEN_HOST}:{LISTEN_PORT}')
    log.info(f'Bridge : {BRIDGE_URL}')
    log.info(f'Point XMRig at: {LISTEN_HOST}:{LISTEN_PORT}')
    log.info('=' * 55)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
