# Connecting the first real PLC - safe cutover procedure

Written for Phase V1.4. Follow this in order the first time a real PLC
is connected, whether that's a full production cutover or just a bench
test against one real device. Every step here uses architecture that
already exists and was validated in Phase V1.4 (see
`FACTORY_AI_DEVELOPMENT_STATUS.md`) - nothing here requires new code.

## 0. Before you start - what you need ready

- The PLC's **protocol**: Modbus TCP, OPC UA, Siemens S7, or Omron FINS.
- **Network reachability** confirmed from this VM to the PLC (same
  subnet/VLAN or routed, firewall allows the protocol's port). This is
  an IT/network action, not something this app can arrange for you.
- Protocol-specific connection details:
  - **Modbus TCP**: host/IP, port (usually 502), unit/slave ID.
  - **OPC UA**: endpoint URL (`opc.tcp://host:port/path`), and whether
    the server requires a username/password or a trusted client
    certificate.
  - **Siemens S7**: host/IP, rack, slot.
  - **Omron FINS**: host/IP, port (usually 9600), PLC node, PC node.
- At least **one real tag address** on that PLC to map to a single
  SmartFactoryAI tag for the first test (e.g. one Modbus register, one
  OPC UA NodeId, one S7 DB/byte/bit, one FINS DM address).
- A decision on **which single piece of equipment** goes live first -
  don't try to onboard the whole plant in one pass.

If any of this is missing, stop here and get it from whoever manages
the PLC/network - don't guess a host, port, or address.

## 1. Confirm you're working in the Actual environment, not Simulation

Open **PLC Connectivity** (admin-only). The "Environment" section at
the top shows which of Simulation/Actual is currently active. If it
says Simulation, switch to Actual and restart `streamlit`,
`plc_logger`, and `event_monitor` before continuing - the Connections
section below only appears in Actual mode, specifically so a real
connection can never be activated while still pointed at the demo
database.

A sidebar badge (added in Phase V1.4) now shows which environment is
active on *every* page, not just this one - green "Simulation" or red
"ACTUAL" - so it stays visible no matter what else you're doing.

## 2. Check the Actual environment for old test data first

As of this phase, `database/actual/` already contains leftover data
from earlier development testing (roughly 10,100 historian rows from
2026-08-12, and again briefly during this phase's own pipeline
validation) - all created against the local OPC UA test simulator
(`/home/test/opcua-web-simulator`), never real hardware. It also has
a handful of placeholder tags (`tes55`, `tes56`, `tes57`, etc.) and an
old connection ("test opc") still marked active, pointing at that test
simulator.

**Decide what to do with this before logging real data**: either clear
it out (delete the placeholder tags/equipment on Equipment & Tag
Configuration, and optionally start `database/actual/` fresh - back it
up first per `FACTORY_AI_DEVELOPMENT_STATUS.md`'s backup/restore
procedure), or at minimum know it's there so nobody mistakes that
Aug-12 block of numbers for real plant history. This app will never
delete it automatically.

## 3. Configure the real equipment and tags

On **Equipment & Tag Configuration**, add the one piece of equipment
you're onboarding first and its tags, the same way as any other
equipment - this is unrelated to which PLC protocol will feed it.

## 4. Create the connection, but don't activate it yet

On **PLC Connectivity** → Connections → "+ New Connection", enter the
protocol and connection details from step 0. This creates the
connection record without making it live.

## 5. Test the connection

Select the new connection and click **Test Connection**. This actually
connects and disconnects using the real driver (`plc/modbus_driver.py`,
`plc/opcua_driver.py`, `plc/s7_driver.py`, or `plc/fins_driver.py`) -
it's the honest way to confirm the PLC is reachable before anything
else depends on it. Fix any network/credential issue here before
moving on; don't proceed on a failed test.

## 6. Map the one real tag address

Under Tag Mapping, select your new equipment and enter the real
address for your one test tag (or use the OPC UA "Browse" picker if
the protocol is OPC UA). Leave every other tag unmapped for now.

## 7. Activate the connection

Back in Connections, click **Set Active**. This only affects the
Actual environment's own connection record - Simulation is untouched
regardless (each environment keeps a fully separate `plc_connections`
table, so this can't cross over).

## 8. Restart plc_logger and confirm real data is flowing

Restart the `plc_logger` service (or, for a bench test, run
`python -m app.plc_logger` manually against the Actual environment).
Within one scan interval, check:

- **Live Data** page shows the new tag's value updating.
- `database/actual/machine_data.db`'s `plc_data` table has new rows for
  that tag (`sqlite3 database/actual/machine_data.db "SELECT * FROM
  plc_data ORDER BY id DESC LIMIT 5;"`).

## 9. Verify the loss/recovery behaviour honestly

Briefly disconnect the PLC (unplug the network cable, or stop the
device if it's a bench test) and confirm:

- No crash - `plc_logger` logs the error and keeps retrying
  (`RECONNECT_INTERVAL_SECONDS = 30` in `app/plc_logger.py` forces a
  reconnect attempt every 30s regardless of the failure mode).
- The **Data Health** page marks that tag "Stale" once its logging
  interval has been exceeded by 5x (or after 300s for tags with no
  configured interval) - it does not keep showing the last value as if
  it were current.
- The PLC Connectivity page's **Per-Tag Read Health** section (Phase
  V2.8) shows that specific tag as 🔴 Failing, with a growing
  consecutive-failure count - this is the narrower, faster signal for
  commissioning: it tells you WHICH tag's read attempt is failing,
  distinct from Data Health's "is this data still fresh" (which can't
  tell a one-tag address problem apart from the whole connection being
  down). If a tag never shows up here at all during a real fault,
  that's worth investigating on its own.

Reconnect the PLC and confirm the tag returns to a fresh/current state
and logging resumes without a restart - the Per-Tag Read Health
section should show it as 🟢 Recovered (the row stays, so the failure
history isn't lost, but `consecutive_failures` resets to 0).

## 10. Only then, expand

Once steps 1-9 are all confirmed for the first tag/equipment, repeat
steps 3, 6 (and 4-5 if a new connection is needed) to add the rest of
that equipment's tags, then the next piece of equipment, and so on -
never jump straight to mapping the whole plant before the first tag
has been proven end-to-end.

## What stays separate, always

- Simulation and Actual are different SQLite files
  (`database/simulation/` vs `database/actual/`) with their own
  `plc_connections` tables - a connection activated in one can never
  be read by the other.
- Switching environments requires a restart of `plc_logger`,
  `event_monitor`, and `streamlit` - it never happens live mid-process,
  so a running process can't silently start writing real data into the
  simulation database or vice versa.
- The simulator itself (`simulator/tag_dataset_model.py` via
  `plc/simulator_driver.py`) is untouched by any of this - switch back
  to it at any time by activating the built-in "simulator" protocol
  or switching environments back to Simulation.
