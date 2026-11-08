# Module Interfaces: ACSM / Adobe-DRM PDF ingestion

**Feature**: `001-acsm-drm-ingestion` | **Date**: 2026-11-07

Contracts for the pack's internal seams. These are the boundaries that make the
system testable; changing a signature here means changing the tests that rely on
it (constitution §IV).

## `acsm._paths`

```python
def inbox_dir(data_dir: Path) -> Path
def extracted_dir(data_dir: Path) -> Path
def text_path_for(data_dir: Path, key: str) -> Path
def sidecar_path_for(data_dir: Path, key: str) -> Path
def read_sidecar(data_dir: Path, key: str) -> dict | None   # None on absence/corruption
def write_cache(data_dir: Path, key: str, text: str, record: dict) -> Path
    # atomic: both files staged as temporaries, then os.replace'd
    # refuses: text.strip() == ""  (raises ValueError)
```

**Guarantees**: `read_sidecar` never raises for a missing, malformed, or
newer-schema file — it returns `None` so callers treat it as "not captured yet".
`write_cache` either leaves both files updated or neither.

## `acsm.capture`

```python
DISPLAY: str                 # ":9"
SCREEN: str                  # "1280x900x24"
WINDOW_TITLE: str            # reader window match; pinned by Phase 0 (A1)
CROP: tuple[int, int, int, int]    # (left, top, right, bottom); pinned by Phase 0 (A3)
BLANK_STDDEV: float          # frame considered unrendered below this

class Display:
    async def __aenter__(self) -> "Display"      # Xvfb, then the window manager
    async def __aexit__(self, *exc: object) -> None

async def find_window(display: str) -> str        # window id; raises if 0 or >1
async def grab_png(display: str, window_id: str) -> bytes
def crop_png(png: bytes, box: tuple[int, int, int, int]) -> bytes
def frame_digest(png: bytes) -> str               # sha256 of cropped pixels
def is_blank(png: bytes) -> bool                  # stddev below BLANK_STDDEV
async def send_turn(display: str, window_id: str) -> None
async def send_start(display: str, window_id: str) -> None
def available_tools() -> dict[str, str | None]    # which binaries resolve, for setup
```

**Guarantees**: every subprocess call uses a fixed argv and an explicit `env`
containing only `DISPLAY` (plus `WINEPREFIX` where relevant); no function here
returns a path to a rendered image — only bytes.

## `acsm.driver`

The seam that carries all loop logic. **No imports from `capture`.**

```python
@dataclass(frozen=True)
class PageCapture:
    index: int
    png: bytes
    digest: str
    text: str = ""       # filled by accept_page; "" when nothing recognised

@dataclass
class DriveResult:
    pages: list[PageCapture]
    ended_by: str        # observed_end | max_pages | stale | empty | cancelled
    restarts: int
    stale_runs: int
    empty_runs: int

    @property
    def complete(self) -> bool      # ended_by == "observed_end" and nothing else

async def drive_document(
    *,
    capture: Callable[[int], Awaitable[bytes]],        # index -> cropped PNG
    send_turn: Callable[[], Awaitable[None]],
    send_start: Callable[[], Awaitable[None]],
    digest_of: Callable[[bytes], str],                 # pixels -> stable digest
    is_blank: Callable[[bytes], bool],                 # rendered yet?
    on_restart: Callable[[], Awaitable[None]] | None = None,
    accept_page: Callable[[bytes], str | Awaitable[str]] | None = None,
    is_last_page: Callable[[bytes], Awaitable[bool]] | None = None,
    page_delay_min: float = 1.5,
    page_delay_max: float = 4.0,
    settle_timeout: float = 30.0,
    stall_timeout: float = 120.0,
    max_pages: int | None = None,
    max_consecutive_stale: int = 4,
    max_consecutive_empty: int = 20,
    max_restarts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> DriveResult
```

`digest_of` and `is_blank` are injected rather than imported so this module
never depends on `capture`; `clock` is injected so the stall watchdog can be
tested without waiting.

**Guarantees**:

- `send_start` is called once before the first capture, and again after every
  restart; a blank frame after `settle_timeout` ends the run as `stale`
  (never as progress).
- A digest equal to the previous digest does **not** create a page; it
  increments the stale counter and re-sends the turn.
- `accept_page` is only called for pages whose digest advanced. `""` from
  `accept_page` increments the empty counter; a non-empty page resets it.
- The loop never sleeps through wall-clock time in tests: all waiting goes
  through `sleep` and all jitter through `rng`.
- `ended_by == "observed_end"` is produced only by `is_last_page` returning
  true; it is never inferred from a threshold.

## `acsm.fulfill`

```python
@dataclass(frozen=True)
class Fulfilled:
    key: str
    key_source: str          # rights_metadata | content_hash
    container: Path
    title: str

class FulfillError(RuntimeError):
    """Reader could not fulfil this loan; message names the operator action."""

def extract_rights_id(container: Path) -> str | None    # no decryption involved
def document_key(container: Path, *, rights_id: str | None = None) -> tuple[str, str]
async def fulfill(acsm_path: Path, *, prefix: Path, display: Display,
                  timeout: float = 300.0) -> Fulfilled
async def wait_for_container(prefix: Path, *, before: set[Path], timeout: float,
                             sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Path
async def find_container(prefix: Path, key: str) -> Path | None   # sidecar-less recovery
```

**Guarantees**: `fulfill` raises `FulfillError` with an operator-action message
for an unactivated prefix and for an expired loan; it never raises a bare
subprocess error. `document_key` never returns an empty string.

## `acsm.acquire`

```python
@dataclass
class ScrapeOutcome:
    key: str = ""
    title: str = ""
    text: str = ""
    page_count: int = 0
    ended_by: str = ""
    restarts: int = 0
    ocr: dict = field(default_factory=dict)
    suspect_pages: list[int] = field(default_factory=list)
    container: str | None = None
    output_path: Path | None = None
    reused_cache: bool = False

    @property
    def complete(self) -> bool       # ended_by == "observed_end"

async def scrape_to_disk(
    *, key: str, container: Path, data_dir: Path, settings: OcrSettings,
    title: str = "", prefix: Path | None = None,
    client: OcrBatchClient | None = None,
    driver_fn: Callable[..., Awaitable[DriveResult]] = drive_document,
    open_document: Callable[[], Awaitable[str]] | None = None,
    page_delay_min: float = 1.5, page_delay_max: float = 4.0,
    max_pages: int | None = None,
    max_consecutive_stale: int = 4, max_consecutive_empty: int = 20,
    quality_floor: float = QUALITY_FLOOR,
    driver_overrides: dict[str, Any] | None = None,
) -> ScrapeOutcome
```

**Guarantees**: writes the cache only when text is non-empty; reports
`output_path=None` otherwise and writes nothing. `open_document` is the reader
seam (start the reader, return its window id) and `driver_overrides` forwards
extra loop arguments — the two hooks that keep this function testable without a
display. Recognition is requested one page at a time (D10) and never reorders
text; no page image is ever written (SC-007).

## `acsm.tools.*`

Every handler:

```python
async def handler(<tool inputs>, context=None, ingestion=None, **_clients) -> dict
```

**Guarantees**: returns a JSON-serialisable dict with a `status` key in
`{"success", "error", "operator_action_required", "already_ingested"}`. Never
raises for an expected failure; the message names the missing prerequisite.
`ingestion is None` yields `{"status": "error", "error_type": "config"}` naming
the ingest permission.

## `acsm.setup`

```python
@dataclass(frozen=True)
class Probe:
    name: str
    found: bool
    detail: str
    remediation: str

def probe_environment() -> list[Probe]
def main(argv: list[str] | None = None) -> int    # never installs anything
```

**Guarantees**: `main` returns non-zero when any required probe is missing; its
output names a remediation command per missing probe; it performs no writes
outside the pack data dir (and none at all in probe mode).

## Vendored OCR modules

Copied from `marginalia-plugin-kindle` at the revision recorded in
`VENDORED_FROM`. Public surface used by this pack:

```python
# acsm/ocr_config.py
@dataclass(frozen=True)
class OcrSettings: ...            # from_env() reads KINDLE_OCR_* variables
MAX_PAGES: int                    # 16
MAX_BYTES: int                    # 32 MiB

# acsm/ocr_wire.py
PROTOCOL: int                     # 1
@dataclass(frozen=True)
class PageImage: page_id: int; png: bytes

# acsm/ocr_remote.py
class OcrBatchClient:
    remote_enabled: bool
    async def recognize(self, pages: list[PageImage]) -> list[str]

# acsm/ocr_local.py
def ocr_image_bytes(image_bytes: bytes, *, crop=None) -> str
```

**Contract**: `recognize` returns exactly one string per input page in input
order, or falls back wholesale to local recognition; it never returns partial
output. The vendored surface MUST NOT be modified locally except for import
paths and the `VENDORED_FROM` marker. This pack calls `recognize` with a single
page per request (D10), so the multi-page batching path is exercised by the
sibling pack's suite rather than by ours; the request-size limits are still the
client's to enforce.
