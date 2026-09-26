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

# Reconnect backoff (bridge drops are routine — Railway edge recycles idle WS)
_BACKOFF_BASE = 2.0
_BACKOFF_MAX  = 30.0
_BACKOFF_MULT = 1.5


async def _pump_bridge(client_reader, client_writer, ws):
    """One bridge session. Returns True if the CLIENT went away (stop),
    False if the BRIDGE dropped (caller should reconnect, worker still good)."""
    done = asyncio.Event()

    async def client_to_bridge():
        """Read raw TCP from XMRig, send as WS binary frames upstream"""
        try:
            while True:
                data = await client_reader.read(8192)
                if not data:
                    return
                await ws.send(data)
        except Exception as e:
            log.debug(f'[client→bridge] {e}')
        finally:
            done.set()

    async def bridge_to_client():
        """Receive WS frames from bridge, write raw TCP back to XMRig"""
        try:
            async for msg in ws:
                data = msg if isinstance(msg, bytes) else msg.encode()
                client_writer.write(data)
                await client_writer.drain()
        except Exception as e:
            log.debug(f'[bridge→client] {e}')
        finally:
            # Pool/bridge side ended. Release the whole session — waiting on
            # the other coroutine here used to park gather() forever, leaving
            # XMRig connected to a dead pipe (hashes go nowhere, looks alive).
            done.set()

    tasks = [asyncio.create_task(client_to_bridge()),
             asyncio.create_task(bridge_to_client())]
    await done.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if client_writer.is_closing():
        return True
    try:
        client_writer.write(b'')      # cheap liveness probe on the worker side
        await client_writer.drain()
    except Exception:
        return True
    return False


async def handle(client_reader, client_writer):
    peer = client_writer.get_extra_info('peername')
    log.info(f'[+] client connected from {peer}')

    delay = _BACKOFF_BASE
    try:
        while True:
            try:
                log.info(f'[→] opening bridge: {BRIDGE_URL}')
                async with websockets.connect(
                    BRIDGE_URL,
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=None,
                    compression=None,   # no compression = lower latency
                ) as ws:
                    log.info(f'[✓] bridge connected')
                    delay = _BACKOFF_BASE
                    client_gone = await _pump_bridge(
                        client_reader, client_writer, ws)
                if client_gone:
                    break
                # Bridge dropped mid-session: reconnect and keep serving the
                # SAME XMRig socket so the miner resumes without a restart.
                log.warning(
                    f'[!] bridge dropped — reconnecting in {delay:.0f}s '
                    f'(worker stays connected)')
                await asyncio.sleep(delay)
                delay = min(delay * _BACKOFF_MULT, _BACKOFF_MAX)
            except Exception as e:
                log.warning(f'[!] bridge error: {e} — retry in {delay:.0f}s')
                await asyncio.sleep(delay)
                delay = min(delay * _BACKOFF_MULT, _BACKOFF_MAX)
                if client_writer.is_closing():
                    break
    except Exception as e:
        log.error(f'[!] fatal: {e}')
    finally:
        log.info(f'[-] client disconnected')
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
