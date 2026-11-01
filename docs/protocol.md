# Fujifilm PC AutoSave protocol (X-E2)

Decoded from the 2016 Wireshark captures in `Documents/Fuji Transfer Capture` (X-E2 firmware 3.0, plus one
X-T1 capture) and cross-checked against [libfuji](https://github.com/petabyt/libfuji) `docs/autosave.md` and
`lib/fujiptp.h` (MIT). Still to verify on current firmware (X-E2 4.x).

## Topology

The camera joins the **home Wi-Fi** (2.4 GHz) and talks to the destination over the LAN. The destination never
joins a camera access point, and needs no Bluetooth. Discovery uses UDP broadcast, so the destination must be in
the same subnet as the camera (no guest network or AP/client isolation).

The camera starts every exchange: the user picks *PC AUTO SAVE* on the camera. The destination cannot start a
transfer.

| Port  | Proto | Listener    | Use                                                |
|-------|-------|-------------|----------------------------------------------------|
| 51542 | UDP   | destination | `DISCOVER *` broadcast while registering            |
| 51542 | TCP   | destination | camera sends `REGISTER`                             |
| 51541 | UDP   | destination | `DISCOVER <name>` broadcast before a transfer       |
| 51541 | TCP   | destination | camera sends `IMPORT`                               |
| 51540 | TCP   | camera      | destination sends `NOTIFY`                          |
| 55740 | TCP   | camera      | PTP command channel (the destination connects)      |

The text messages look like HTTP but are not: lines end in `\r\n`, methods are custom (`DISCOVER`, `NOTIFY`,
`REGISTER`, `IMPORT`). A real HTTP server library rejects them (`Registering.pcapng` shows Apache HttpCore
answering `501 REGISTER method not supported`, and registration failing). Use plain sockets.

## Registration (camera menu: PC AUTO SAVE SETTING > register destination)

1. Camera → UDP broadcast :51542
   ```
   DISCOVER * HTTP/1.1
   MX:3
   HOST:192.168.20.255:<junk byte>
   DSCADDR:192.168.20.20
   SERVICE:PCAUTOSAVE/1.0
   ```
2. Destination → TCP camera:51540, camera answers `HTTP/1.1 200 OK`
   ```
   NOTIFY * HTTP/1.1
   HOST: 192.168.20.20:51540
   IMPORTER: Stockholm
   <empty line>
   ```
3. Camera → TCP destination:51542
   ```
   REGISTER * HTTP/1.1
   HOST:192.168.20.18:51542
   DSCNAME:FUJIFILM-X-E2-BD35
   DSCMODEL:X-E2
   ```
   Destination answers
   ```
   HTTP/1.1 200 OK
   FOLDER: guest
   ServiceName: PCAUTOSAVE/1.0
   ```
   The camera stores the `IMPORTER` name. `DSCNAME` identifies the camera.

## Transfer start

1. Camera → UDP broadcast :51541: `DISCOVER Stockholm HTTP/1.1` (same headers as above). Only the destination
   with that `IMPORTER` name should answer.
2. Destination → `NOTIFY` to camera:51540 as above, camera answers `200 OK`.
3. Camera → TCP destination:51541 (in the captures about 12 s later)
   ```
   IMPORT /guest HTTP/1.1
   HOST:192.168.20.17:51541
   DSCNAME:FUJIFILM-X-E2-BD35
   DSCPORT: 
   ```
   Destination answers `HTTP/1.1 200 OK\r\n\r\n`, then connects to camera:55740.
   The camera opens port 55740 only a moment later and refuses connections until then (seen from the Pi: refused
   10 ms after the IMPORT answer). Windows retries a refused connect by itself after ~0.5 s, so PC AutoSave never
   notices; on Linux the destination must retry. If nobody connects, the camera stays on "Checking files to be
   saved" until cancelled.

## PTP channel (TCP 55740)

Fuji does not use standard PTP/IP framing after the init packet. Every packet is a PTP-over-USB container,
little-endian:

| Offset | Size | Field                                              |
|--------|------|----------------------------------------------------|
| 0      | 4    | total length including this header                 |
| 4      | 2    | type: 1 command, 2 data, 3 response, 4 event       |
| 6      | 2    | opcode (command/data) or response code             |
| 8      | 4    | transaction id, starts at 1 with OpenSession       |
| 12     | n    | parameters (u32 each) or data payload              |

A command with outgoing data (SetDevicePropValue) is followed by a data packet with the same opcode and id.
Every transaction ends with a response packet; `0x2001` is OK.

**The data packet must arrive after the camera acknowledged the command.** The camera drops whatever comes in
the same read as the command and then waits forever for the data. PC AutoSave gets this for free from Windows'
default Nagle algorithm (its data leaves ~100 ms later, after the camera's TCP ACK). Sending both in one write, or
in two writes with `TCP_NODELAY` (asyncio's default), stalls the session at the first SetDevicePropValue.
Verified on the real X-E2 (firmware 4.11): fails without Nagle, works with it.

### Init

- Destination → `len=0x52, type=1`, then u32 `0x8f53e4f2` (protocol version), 16-byte GUID (the last 4 bytes in
  the captures are the PC's IPv4 address reversed), the camera's `DSCNAME` as zero-terminated UTF-16LE, padding.
- Camera → `len=0x44, type=2`, u32 0, 16-byte camera GUID, model name (`X-E2`) as UTF-16LE, padding.

### Session as done by PC AutoSave

| #  | Request                                      | Answer / meaning                                              |
|----|----------------------------------------------|---------------------------------------------------------------|
| 1  | `OpenSession(1)` 0x1002                      | OK                                                            |
| 2  | `GetDevicePropValue(0xD212)` 0x1015          | events list: `DF00=3` (camera state: PC AutoSave), `D222=N` (number of objects) |
| 3  | `SetDevicePropValue(0xDF01, u16 3)` 0x1016   | client state "old remote" / AutoSave                          |
| 4  | `GetDevicePropValue(0xDF23)`                 | AutoSave version, `1`                                         |
| 5  | `SetDevicePropValue(0xDF23, u32 1)`          | echo the version back                                         |
| 6  | `SetDevicePropValue(0xD227, u16 1)`          | "enable correct file size"                                    |
| 7  | `GetObjectInfo(h)` 0x1008 for h = 1..N       | standard PTP ObjectInfo                                       |
| 8  | `SetDevicePropValue(0xD228, "0/K")`          | progress text shown on the camera, K = files to transfer      |
| 9  | per file: `GetObjectInfo(h)`, then `GetPartialObject(h, offset, 0x100000)` 0x101B until done | file bytes in 1 MiB chunks |
| 10 | after each file: `SetDevicePropValue(0xD228, "i/K")` |                                                       |
| 11 | `SetDevicePropValue(0xD227, u16 0)`          | the camera answers with `08 00 00 00 ff ff ff ff` and closes (see below) |

After a complete transfer (`D228` = `K/K`), the camera answers the `SetDevicePropValue(0xD227)` *command* with
the end marker `08 00 00 00 ff ff ff ff` within a few milliseconds, without waiting for the data phase (PC AutoSave
still sends it, then closes). After an error (`D228` = `i/K:ERR(-1)`) the camera answers `D227=0` with OK; PC
AutoSave then sends `CloseSession` 0x1003 (OK), sends the end marker **itself**, and closes. PC AutoSave polls `GetDevicePropValue(0xD212)` between steps (empty list = no
events); treat it as a keep-alive. libfuji notes an error variant of the progress string: `0/60:ERR(-1)`.

### End of session and cancel

`08 00 00 00 ff ff ff ff` means "session over", from either side. The camera sends it in place of the answer to
the final `D227=0` after a complete transfer, and also when the user presses cancel on the camera: then it
replaces the pending response (2026-10-31 capture, session 1:
cancelled in the middle of `GetObjectInfo` number 111 of 4581). The destination should close the socket and treat
an unfinished file as interrupted.

### Events list (0xD212 value)

u16 count, then count × (u16 property code, u32 value).

### Object handles and "what is new"

- The camera exposes **every** picture on the card, handles `1..N` (`N` from `D222`). It does not track what was
  transferred: in `NoNew.pcapng` PC AutoSave reads all 12 ObjectInfos and downloads nothing.
- Handles are positions, not stable ids: handle 9 was `DSCF4558.JPG` in one session and `DSCF4563.JPG` in the
  next. The order is not chronological.
- So the destination keeps its own list, keyed by camera `DSCNAME` + filename + capture date + size.
- New photos normally get the last handles. The handle 9 example looks like a file shot after a deletion filling
  the freed place, so after deleting photos in the camera, new ones can sit anywhere. fuji_sync reads newest first
  and stops at already copied files (quick scan), then checks the file count and the card's free space against the
  previous session and reads the whole card if they don't add up (`SyncService._scan`).

### ObjectInfo (data of 0x1008)

Standard PTP layout: storage id, u16 format (`0x3801` JPEG, `0xB103` Fuji RAF, `0x300D` MOV), u32 compressed
size at offset 8,
then at offset 52 PTP strings (u8 char count incl. terminator, UTF-16LE): filename (`DSCF4492.JPG`), capture date
(`20160225T211038`, camera local time), modification date (empty), keywords (`Orientation:1`).

### Throughput

About 0.75–1 MB/s in the captures (a 33 MB RAF takes 35–45 s). The fujiautosave library got about 1.5 MB/s
on the first real-camera run (4 JPEGs and a 29 MB MOV, 34 MB in 23 s, firmware 4.11, same Wi-Fi).

## 2026-10-31 capture (PC AutoSave current version, camera still on firmware 3.00)

`2026-10-31 test 3img 1 mov severed.pcapng`: camera 192.168.1.228, PC 192.168.1.43. Two registrations, then three
transfer sessions. The protocol is the same as in 2016.

1. Card with 4581 pictures: PC AutoSave reads ObjectInfo one by one (about 4 per second); the user cancels on the
   camera after 111.
2. Empty card plus 3 JPG and 1 MOV (`d222=4`). The JPGs are downloaded newest first (handles 3, 2, 1), progress
   `0/4` to `3/4`. Then `DSCF2089.MOV` (34,277,120 bytes) is downloaded completely, and PC AutoSave reports
   `3/4:ERR(-1)` and shows "severed" on the camera.
3. Retry: PC AutoSave only asks for the MOV (`0/1`): it remembers the JPGs itself. Same result.

Rebuilt from the capture, the MOV is complete and well-formed (`ftyp` 20 + `moov` 1,017,252 + `mdat` 33,259,848 =
the reported size), and both downloads are byte-identical. **The camera serves movies correctly; the error comes
from PC AutoSave itself.** Our app can save movies.

Listing cost: on a full card the listing alone takes about 20 minutes (4 ObjectInfo/s). Keep the last listing per
camera and only re-read when `d222` or the first/last entries changed (to verify).

## 2026-10-31 capture after firmware update (X-E2 firmware 4.11)

`2026-10-31 after update.pcapng`: one transfer session, card with 4 JPG and 1 MOV (`d222=5`). Byte for byte the
same protocol as firmware 3.00: same handshake, same init packets (the camera GUID is unchanged), same property
sequence. The JPGs are downloaded newest first (`1/5` to `4/5`), then `DSCF2094.MOV` (29,460,416 bytes) arrives
completely and PC AutoSave answers `4/5:ERR(-1)` and `CloseSession`, as before.

The rebuilt MOV is again well-formed (`ftyp` + `moov` with a video and an audio track + `mdat`, sizes add up). So
the firmware update changes nothing for us, and the movie error is still on the PC AutoSave side.

## Open points

- Whether ObjectInfo can be read faster on a full card (pipelining, or GetObjectHandles 0x1007 /
  GetObjectPropList, which PC AutoSave never uses).
- Camera timeout when the destination is slow (NAS write stalls).
- Whether `GetDeviceInfo` (serial number) is allowed in AutoSave mode; PC AutoSave never calls it.
- Whether the camera really puts new files into freed places (the handle 9 observation), and whether file order
  otherwise follows shooting order on a card with many folders. On 2026-11-01 (firmware 4.11), deleting 2 photos and
  shooting 2 put the new ones at the end (handles 10 and 11 of 11); the count check saw the deletion and read the card.
  Deleting DSCF2097 from the middle of the card and shooting one also put the new photo (DSCF2105) at the end. So
  with firmware 4.11 new files seem to be appended; the 2016 handle 9 observation (firmware 3.00) is unexplained.

`GetStorageInfo` (0x1005), which PC AutoSave never calls, is answered with OK but no data at all in AutoSave mode
(X-E2, firmware 4.11, 2026-11-01). So there is no free space to check, and the quick scan only has the file count:
deleting n photos and shooting n that fill exactly the freed places goes unseen until the next full scan.
- The camera normally sends IMPORT about 11–13 s after our NOTIFY. On 2026-10-31 it twice sent DISCOVER, accepted
  the NOTIFY and then never sent IMPORT (no TCP connection to 51541 at all); the third try right after worked. Both
  were the first tries after the camera had searched for a long time while no destination was listening. Not captured;
  if it recurs, capture it.
