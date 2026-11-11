# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through **GitHub's private vulnerability reporting**: open the repository's **Security** tab and choose **Report a vulnerability** ([new advisory](https://github.com/Bto-03/hermes-flota/security/advisories/new)). Don't open a public issue.

Include the Flota and Hermes versions, the OS of the flagship and the ship, and steps to reproduce. You'll get an acknowledgement within a week.

## Supported versions

Only the latest release receives fixes.

## Scope

In scope: anything that lets a pairing code, a ship, the dashboard API or a crafted config do more than [docs/security.md](docs/security.md) says Flota does. Examples: shell injection through a machine setting, a ship code that adds an unintended host, or credential leakage onto a ship.

Out of scope: the fact that an authorised flagship key gives a normal shell on the ship (that's the design), and Wake-on-LAN packets from the local network.
