# AI Mandates for Tawspom

## 1. Source of Truth
- **SPEC.md** is the foundational specification for this project.
- You MUST read **SPEC.md** at the start of every session to align with the project architecture, constants, and logic.
- If instructions given are such that there is possibility to misunderstanding, ask clarification instead of guessing.
  Asking and suggesting possible ways to proceed is in any case more valuable than going to implementing right away

## 2. Documentation Maintenance
- You MUST update **SPEC.md** immediately after any code change to ensure it reflects the current state of the application.
- Never truncate or delete sections of **SPEC.md** unless specifically instructed to refactor that feature.
- Code should have small amount of comments to help read and understand code. Comments should not be removed without proper 
  reason
- Avoid doing extra changes that are not really related to requested change. Extra changes make reading diff harder, and 
  make your work less valuable. If you think there would be need for changes/refactoring, suggest it but don't do it silently

## 3. Engineering Constraints
- Respect the magic-number-to-constant migration.
- Always use the constants defined in `tawspom/core/constants.py`.
- Prioritize library integrity and safety checks (e.g., mass-deletion confirmation).
