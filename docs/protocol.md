# The wire protocol

> Read this when: you are writing a client, or a request is not getting an answer.
> Everything a command takes and returns is in [commands.md](commands.md).

## Transport

A Unix domain socket. The emulator binds it when started with
`--agent-socket <path>`; every connected client shares one machine.

`sun_path` holds **108 bytes**. A longer path fails to bind, so keep the
socket in `/tmp`, not in a deep scratch directory.

More than one client may connect. Each gets its own replies, told apart by the
`id` it sent, and one disconnecting does not disturb the others.

## Framing

One JSON object per line, `\n`-terminated, in both directions. `\r\n` is
accepted. Requests may be pipelined; replies come back in order.

A request line longer than **4 MB** is not answered — the client is dropped,
because the line never completed and so nothing was ever dispatched. This is
the one failure that is not a JSON error reply.

## Request

```json
{"id": 7, "cmd": "mem_read", "params": {"addr": 55424, "len": 4, "view": "cpu"}}
```

| field | type | required | meaning |
|---|---|---|---|
| `id` | any | recommended | reflected verbatim in the reply; `null` if omitted |
| `cmd` | string | yes | command name |
| `params` | object | no | per-command parameters |

## Response

Success and failure are the same shape apart from one key:

```json
{"id": 7, "ok": true,  "result": {"addr": 55424, "len": 4, "view": "cpu", "data": "qur+/w=="}}
{"id": 7, "ok": false, "error": "view must be cpu, main or aux"}
```

- `ok: true` carries `result` and never `error`.
- `ok: false` carries a non-empty `error` string and never `result`.
- Unparseable input still gets a reply (`"malformed JSON request"`), so a
  client can always tell a refusal from a hang.

## What a refusal means

A parameter outside its range is refused rather than clamped — an out-of-range
`addr` would otherwise be a wild read. But **`ok: true` is not proof that the
machine did what you meant**:

- `disk_insert` answers `ok: true` for a path that does not exist. Whether a
  disk actually went in is in `drives[n].loaded` of the reply.
- `key_*` and `joy_*` queue the input. It is applied when the machine next
  runs, and the running program sees it **one frame after that** — see
  [timing.md](timing.md).

## Discovery

`version` returns `{protocol, emulator, frontend}`; this document describes
protocol **1**. `commands` returns the names this build actually answers, which
is the check to make before using anything below.

```bash
socat - UNIX-CONNECT:/tmp/linapple-agent.sock
{"id":1,"cmd":"version"}
{"id":2,"cmd":"commands"}
```
