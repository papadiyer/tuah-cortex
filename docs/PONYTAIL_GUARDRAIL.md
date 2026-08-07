# Ponytail — Lazy Senior Dev Mode (Build Guardrail)

**Owner:** Jebat (CTO) · **Applies to:** All coding delegation to Lekiu
**Principle:** *The best code is the code never written.* Lazy means efficient, not careless.

Before writing ANY code, stop at the first rung that holds:

1. **Does this need to be built at all?** (YAGNI)
2. **Does it already exist in this codebase?** Reuse the helper, util, or pattern already here — don't rewrite it.
3. **Does the standard library already do this?** Use it.
4. **Does a native platform feature cover it?** Use it.
5. **Does an already-installed dependency solve it?** Use it.
6. **Can this be one line?** Make it one line.
7. **Only then:** write the minimum code that works.

## Why this exists

We run on limited compute/credit. Wasting tokens building something that already
exists (in-repo, stdlib, platform, or installed dep) is a resource leak AND a
quality risk — more code = more surface for bugs, more to maintain.

## Jebat (CTO) pre-delegation checklist

Before handing a task to Lekiu, Jebat verifies rungs 1–3 himself:
- Is this actually needed, or scope creep? (YAGNI)
- Is there an existing helper/pattern in the target repo? (reuse)
- Can stdlib/platform cover it? (no new dep)

If Jebat skipped a rung, he says so explicitly in the task brief.

## Lekiu (Builder) binding

When Lekiu receives a coding task, before editing:
1. Read project `CLAUDE.md` + this guide.
2. For each requested piece, check rungs 1–7 in order.
3. If an existing util/stdlib/platform/dep already does it, **use it** — do not
   reimplement. Cite what you reused in the completion report.
4. Only write the minimum that passes tests. No gold-plating.

## Report requirement

Every completion must state: *"Reused: <existing thing> / Stdlib: <module> /
New code: <min lines>."* If Lekiu wrote something that already existed, that's a
miss — flag it.

## Red lines

- Never add a dependency to avoid 5 lines of stdlib.
- Never rewrite an in-repo helper "cleaner" — reuse it.
- Never build a framework for one call site.
