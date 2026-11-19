# Vendored third-party code

## ccl_segb/
Apple SEGB (Biome) file parser by Alex Caithness / CCL Solutions Group.
Source: https://github.com/cclgroupltd/ccl-segb  (MIT license, see ccl_segb_LICENSE.txt)

Vendored (not pip-installed) so the Screen Time collector stays self-contained
and runs under the stock macOS `/usr/bin/python3` with no external dependencies.

LOCAL MODIFICATIONS (each tagged `# LOCAL EDIT` in-file), needed only for
Python 3.9 compatibility (upstream targets 3.10+):
  - added `from __future__ import annotations` (PEP 563) so PEP 604 `X | Y`
    type hints in signatures don't evaluate at runtime
  - dropped `kw_only=True` from two `dataclasses.field(...)` calls (3.10+ only);
    those fields are always passed by keyword at construction, so behaviour is
    identical.
No parsing logic was changed. To update: re-copy from upstream and re-apply the
two edits above.
