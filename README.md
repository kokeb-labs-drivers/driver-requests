# Driver requests

Ask for a chip driver, a port to a new MCU family or target, or report a datasheet erratum. This repository holds
only the requests; the drivers live in their own repositories in
[kokeb-labs-drivers](https://github.com/kokeb-labs-drivers).

| I want to | Open |
| --- | --- |
| A driver for a chip that has none | [Request a driver](https://github.com/kokeb-labs-drivers/driver-requests/issues/new?template=request-driver.yml) |
| The drivers on an MCU family, SDK or RTOS without a port | [Request a port or target](https://github.com/kokeb-labs-drivers/driver-requests/issues/new?template=request-port.yml) |
| Report that a chip does not do what its datasheet says | [Report a datasheet erratum](https://github.com/kokeb-labs-drivers/driver-requests/issues/new?template=datasheet-erratum.yml) |
| Ask a question or share an idea | [Discussions](https://github.com/kokeb-labs-drivers/.github/discussions) |
| Report a bug in an existing driver | That driver's own repository |
| Report a vulnerability | Privately, per the [security policy](SECURITY.md) |

Blank issues are off: every request uses a form, so it can be checked automatically and compared with the others.

## Before you ask

1. Search the [open and closed requests](https://github.com/kokeb-labs-drivers/driver-requests/issues?q=is%3Aissue)
   and the [existing drivers](https://github.com/orgs/kokeb-labs-drivers/repositories?q=driver-).
2. If the chip or target is already requested, add a 👍 reaction to that issue. That is the vote. A "+1" comment
   does not count and notifies everyone watching; please use the reaction.
3. One chip per request. A family with one datasheet (for example a part with 16- and 32-bit variants) is one chip.

## The datasheet link

A driver is written only from public documents. The link in a request must be:

- an `https` link to the product page or the datasheet **on the chip vendor's own website** (not a distributor, a
  datasheet aggregator, a file sharing site or a search result);
- reachable without an account, a login or a signed agreement;
- a link, never an attachment: do not upload the PDF or any other file to the issue;
- never a document under NDA or marked confidential, even if you are allowed to read it.

The triage check compares the host with the vendor list in [`tools/triage/vendors.txt`](tools/triage/vendors.txt)
and labels the request `needs-datasheet` otherwise. If the vendor is missing, say so in a comment and a maintainer
adds the domain. The link is only parsed: the check never opens it.

## What happens to a request

Every request is on the board [Driver pipeline](https://github.com/orgs/kokeb-labs-drivers/projects). Its *Stage*
field shows where it is:

| Stage | Meaning |
| --- | --- |
| Requested | Opened, not yet looked at by a maintainer |
| Triaged | The part is clear, not a duplicate, and fits the drivers' scope |
| Datasheet acquired | The public documents are on file and cover the whole chip |
| In design | The architecture note is being written |
| Building | The driver, its tests and its docs are being written |
| In review | Independent review rounds and the full local CI rehearsal |
| Released | A release candidate is out; the issue is closed as completed |
| Declined | Closed as not planned, with the reason in a comment |

When the request is opened or edited, the triage workflow:

- reads the form's fields and the part number, normalised to upper case without package or reel suffixes, and
  labels the issue `chip:<PART>` (for example `chip:TMP117`);
- lists in one comment the other requests with the same label and the existing driver repositories that may cover
  the part (nothing is closed automatically: a maintainer closes duplicates);
- labels `needs-datasheet` when the link is not `https` on a vendor domain, and `needs-info` when the part number
  cannot be read or a confirmation box is not ticked. Editing the issue re-runs the check and clears the label.

A request that is a datasheet erratum is moved to the driver's repository after triage, because it changes that
driver.

## How requests are judged

The queue is ordered by:

1. **Votes**: 👍 reactions on the issue.
2. **Documents**: complete public datasheets and application notes. A chip whose register map is only available
   under NDA cannot be done.
3. **Effort**: simpler chips go first (one bus, a small register map), so the queue keeps moving while large
   chips wait for review capacity.
4. **Help offered**: someone who can test on hardware or review moves a request up.

A request may be declined when the documents are not public, the part is obsolete or not recommended for new
designs, or it is a module whose firmware already hides the chip. The reason is always given.

## Labels

| Label | Set by | Meaning |
| --- | --- | --- |
| `driver-request`, `port-request`, `erratum` | The form | The kind of request |
| `chip:<PART>` | Triage | The normalised part number |
| `needs-datasheet` | Triage or a maintainer | The datasheet link is missing, not on a vendor domain, or an upload |
| `needs-info` | Triage or a maintainer | A required answer is missing or unreadable |
| `needs-hardware` | A maintainer | Waits for someone who can test on hardware |
| `duplicate` | A maintainer | Closed in favour of another request |
| `agent:ready`, `agent:claimed`, `agent:blocked` | Maintainers | Build queue state |

## Working on this repository

The triage script is Python with the standard library only. Run its tests and a dry run locally:

```sh
python3 -m unittest discover -s tools/triage -v
python3 tools/triage/triage.py --event path/to/issues-event.json --dry-run
```

A dry run reads public data from the GitHub API and prints the labels and comment it would write.
Contributions follow the organisation's
[contributing guide](https://github.com/kokeb-labs-drivers/.github/blob/main/CONTRIBUTING.md); every commit is
signed off (`git commit -s`). Conduct: [code of conduct](CODE_OF_CONDUCT.md). Contact: <kokeblabs@gmail.com>.

## Licence

[MIT](LICENSE).
