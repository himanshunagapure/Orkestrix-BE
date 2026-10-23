# Design reference integration

In-process API over the vendored **ui-ux-pro-max** design engine.

| Item | Location |
|---|---|
| Pin | **v2.11.0** (`6142b07`) |
| Vendored assets | `backend/design/design_reference/` (data CSVs, `engine/core.py`, `engine/design_system.py`, LICENSE) |
| Integration API | this package (`services.design_reference`) |

Do **not** import `search.py` or shell out to the CLI. Call
`resolve_theme_preferences(...)` from `ai_processor` after prompt enhancement.

See `backend/design/design_reference/VENDORED_FROM.md` for update instructions.
