# AI Mandates for Tawspom

## 1. Source of Truth
- **SPEC.md** is the foundational specification for this project.
- You MUST read **SPEC.md** at the start of every session to align with the project architecture, constants, and logic.

## 2. Documentation Maintenance
- You MUST update **SPEC.md** immediately after any code change to ensure it reflects the current state of the application.
- Never truncate or delete sections of **SPEC.md** unless specifically instructed to refactor that feature.

## 3. Engineering Constraints
- Respect the magic-number-to-constant migration.
- Always use the constants defined in `tawspom/core/constants.py`.
- Prioritize library integrity and safety checks (e.g., mass-deletion confirmation).
