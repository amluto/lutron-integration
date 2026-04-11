# lutron-integration-tools

Command-line tools for [lutron-integration](https://github.com/amluto/lutron-integration).

## Installation

There is generally no need to install this per se.  Use uv!

## Usage

### lutron_monitor

Monitor unsolicited device updates from a Lutron QSE-CI-NWK-E hub:

```bash
lutron_monitor [-u USERNAME] [--record FILE] IP_ADDRESS
```

Examples:
```bash
lutron_monitor 192.168.1.100
lutron_monitor -u admin 192.168.1.100
lutron_monitor --record session.json 192.168.1.100
```

You will be prompted for a password.

If `--record` is specified, `lutron_monitor` writes a JSON recording of the raw
socket traffic. Each byte is stored as the corresponding JSON string code point,
so ordinary ASCII sessions remain readable while still round-tripping arbitrary
byte values without external dependencies.
