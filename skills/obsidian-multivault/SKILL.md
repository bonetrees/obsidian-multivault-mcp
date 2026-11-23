---
name: obsidian-multivault
description: "Use before reading, writing, searching or editing notes in an Obsidian vault through the obsidian-multivault-mcp server (tools list_vaults, read_note, write_note, append_note, patch_note, delete_note, list_directory, search_vault, search_all_vaults), including requests like 'put this in my vault' or 'add this to my notes'. Covers vault discovery and Index.md, etags and if_match, relative heading levels, sibling inserts, child-heading protection, frontmatter values, and what each refusal means."
---

# Obsidian Multivault MCP

This server wraps the Obsidian Local REST API plugin across several vaults. It isn't `mcp-obsidian` or any other Obsidian server: tool names and `patch_note` semantics differ, so ignore guidance written for those.

Most mistakes here produce a **refusal with nothing written**, and the error message says what to do instead. Read it and follow it; don't retry the same call.

## Orient first

- **`list_vaults`** when the vault isn't known. Pass a vault's `name` as `vault` everywhere else. Each vault's `status` tells you whether to go on:
  - `online`: ready.
  - `unreachable`: Obsidian is closed, the plugin is off, or the port is wrong. Tell the user; don't retry or try other reads.
  - `degraded`: reachable, but `Index.md` couldn't be read, so other reads may fail too. The reason is in `error`.
- **Paths are relative to the vault root**, e.g. `projects/plan.md`. A folder on disk is usually a folder *inside* a vault, not a vault of its own.
- To find things, use **`search_vault`** or, for a topic in an unknown vault, **`search_all_vaults`**, and **`list_directory`**. Don't guess paths. `search_type` is:
  - `text` (default): full-text search;
  - `jsonlogic`: `query` is a JSON string, e.g. `'{"in": ["#project", {"var": "tags"}]}'`. Use it for frontmatter, tags and paths; `glob` and `regexp` are available too, e.g. `'{"glob": ["projects/*", {"var": "path"}]}'`.

  There's no Dataview (DQL) search: the REST API plugin doesn't offer it.

### Index.md: the vault's map

Each vault may keep an `Index.md` at its root, written so agents can find their way around. `list_vaults` returns its content; only `Index.md` (capital I) at the root is read, and a vault without one has `index: null`, which isn't an error.

- **Read it before working in a vault, and follow its pointers.** If it says to read a file first, do that before anything else.
- **Its format:**

  ```markdown
  # Vault Name

  One-paragraph description of what the vault holds and what it's for.

  ## Folders

  ### folder-name
  What's in it, when to look here, any naming conventions.

  ## Key Files

  - `path/to/file.md` — what it is and when to use it
  ```

- **Update it** when you add a folder or key note that other sessions will need. Add an entry under the matching section (a `### folder` under Folders, or a bullet under Key Files), never at the end of the file. Pass `if_match` like any other write: `patch_note` with `append` on `"Vault Name::Key Files"` adds a bullet cleanly.

## Read, then write with the etag

- `read_note` returns the body **with the frontmatter stripped**, the parsed `frontmatter` separately, plus `tags`, `stat` and an **`etag`**. Every write tool returns the note's new `etag`.
- **Pass `if_match`** with the etag from your latest read or write on every change to an existing note: `write_note`, `append_note`, `patch_note` and `delete_note`. Someone may be typing in the same note in Obsidian.
- **On "Precondition failed"**, the note changed since you read it, and nothing was written. Re-read, re-apply your change to the current content, and retry with the new etag. Never retry blind.
- **`if_match` also makes retries safe.** If a write's result got lost, resend it with the *same* `if_match`. If the first attempt landed, the note's version moved, so the retry fails with "Precondition failed" instead of adding a second copy. Then re-read to check.
- **New notes:** `write_note(..., create_only=True)`, so an existing note is never overwritten. Frontmatter goes at the top of `content` between `---` fences.
- **Full rewrites** (`write_note` without `create_only`) replace the whole file. Prefer `patch_note` whenever only part of the note changes. When you must rewrite, build the content from a fresh read, and **put the frontmatter back**: rebuild the `---` block from the returned `frontmatter` and put it above the body. A body-only rewrite silently deletes every property.
- **`append_note` and `patch_note` append add no newline.** Start `content` with `\n`.

## patch_note: pick the operation by intent

`target_type` is `heading`, `block-reference` (the block id without `^`) or `frontmatter-key`.

| You want to… | Call |
|---|---|
| Add a **new section** (log entry, decision, dated note) next to existing ones | **`insert_after`** the last existing one (after its whole section, children included), or **`insert_before`** the first, with content opening `# Title` |
| Add text at the end of a heading's section | `append`. It lands **after the last existing child heading**. |
| Add text right below a heading line | `prepend` |
| Change a parent's intro text (above its first child heading) | `replace` / `append` / `prepend` with **`within=<block index>`** |
| Change one child section | Target the child itself: `Parent::Child` |
| Replace a whole section, children included | `replace` with `replace_children=True` |
| Remove a section, block or key | `delete` (see below) |
| Set a frontmatter key | `frontmatter-key` with `content` (a string) or `value` (structured) |

### Heading targets are full paths

`target` is the **full path from the document root**, joined with `::`. For `# Report` > `## Findings` > `### Critical`, that's `"Report::Findings::Critical"`. A bare `"Critical"` fails with a 404. If a heading's own text contains `::`, pass the path as a list: `["Report", "Q1::Q2 summary"]`. Read the note first if you're unsure of its structure.

**Only the first of a repeated title is reachable.** If a note already has two `## Update` headings under one parent, a path reaches only the first. To change a later copy, read the note and `write_note` the whole thing with `if_match`, putting the frontmatter back as above.

### Heading levels in content are relative

The plugin rebases headings inside `content`:

- For `append` / `prepend` / `replace`, **`#` is a direct child** of the target and `##` a grandchild. Under `### Critical`, write `# Details` to get `#### Details`.
- For `insert_after` / `insert_before`, **`#` is a sibling** of the target, at the target's own level.
- **`within` content is not rebased:** it's spliced literally into one block, so it **must not contain heading lines at all**, and any that it does are refused. Use `append` or `insert_after` to add sections.

**Never copy the `#` count you see in the note.** Content whose shallowest heading has 2+ `#`s is refused ("looks copied from the note's absolute levels"), and so is content with a heading that would land past h6. Fix the levels and retry. Pass `allow_deep_headings=True` only when the deeper nesting really is intended.

### New sections: unique titles

- **`insert_after` / `insert_before` content must open with a `# Title` line.** Plain text would silently join the previous section, so it's refused.
- **A new section's title must be unique under its parent.** This is checked for every `#` heading in the content of `insert_after`, `insert_before`, `append` and `prepend`. The check covers the parent's existing children and the other headings in the same content. A clash is refused, and the message suggests a free title (`'# Update (3)'` or `'# 2026-11-22 Update'`). For recurring entries, put a date or number in the title from the start.
- The same refusal makes a **retry safe**: if a write you sent earlier already succeeded, the retry is refused with "there is nothing to do". Don't rename the section and send it again.

### Replacing a heading never silently deletes children

A heading's content is its **whole subtree**. So a `replace` on a heading with child headings is refused, and the error names the children. Choose:

- **`within`** to edit only the intro: an index into the top-level blocks (paragraphs, lists, tables…) **above the first child heading**. `0` is the first block and `-1` the last. `replace` swaps that block. `append` / `prepend` splice literally onto its end or start, so `append` with `"\n- new item"` and `within=-1` extends a list. Children are never touched. An out-of-range index fails and names how many blocks there are. No heading lines in the content.
- **Target the child** directly to change one child.
- **`replace_children=True`** only to really replace the whole section, children and all.

### Frontmatter

- **`content` is always written as a YAML string**: `"1.0"`, `"true"` and `"null"` stay strings. Content that looks like a JSON list or object is refused.
- **`value`** is for structured data: lists, objects, numbers and booleans, e.g. `value=["project", "urgent"]`, `value=2`. Pass the value itself, not a string containing it.
- `append` / `prepend` merge: two lists concatenate (duplicates kept), two objects shallow-merge (new keys win on `append`, existing keys on `prepend`), two strings concatenate. Mixed types are refused; use `replace` to change a value's type.
- A missing key fails unless you pass **`create_if_missing=True`**, which also creates the frontmatter block if the note has none.

### Delete

`operation="delete"` takes no `content`. Read first and pass `if_match`. `delete_scope`:

- **`all`** (default): the whole target. For a heading, that's the heading line, its body and **every child heading**; for a block, the block and its `^id`; for a key, the key (removing the last key removes the `---` block).
- **`content`**: empty it but keep the anchor. A heading keeps its line, but its body **and children** still go; a block keeps its `^id`; a key is set to `null`.
- **`marker`**: only the anchor. The heading line goes, its body joins the section above, and **its child headings may move up a level** (`###` becomes `##`). For a block, only the `^id` goes. Not valid for frontmatter.

`delete_note` deletes a whole note. It needs `confirm=True`, and you should **check with the user first**.

## Warnings vs. refusals

- **A refusal raises an error and writes nothing.** This covers bad levels, overflow, headings in `within` content, a headless insert, a duplicate title, a replace with children, a stale `if_match` and invalid argument combinations. Follow the message.
- **`warnings` on a successful result** are advisory notes from the plugin about a patch that **was applied**, e.g. `heading-depth-overflow` when `allow_deep_headings=True` let a heading pass h6. Check whether the result is what you meant.

## Before you finish

- Did every change to an existing note pass `if_match` (or use `create_only` for new notes)?
- Did every full rewrite keep the note's frontmatter?
- Did you **read back** notes whose headings you touched, and check that the rest survived?
- If you created a note or folder that other sessions will need, is it in the vault's `Index.md`, under the right section?
