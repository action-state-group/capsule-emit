# Tutorials

Short, copy-paste walkthroughs. No standards background needed — if you can run
`pip install` and call a function, you can do all of these. Each one is a real
terminal session you can reproduce line for line.

**New here? Start at the top and go in order.**

1. [Your first capsule](01-your-first-capsule.md) — seal one action, see what you
   get back. ~5 minutes.
2. [Confirming & chaining](02-confirming-and-chaining.md) — link "I did it" to "it
   actually went through," the way a human-in-the-loop approval works.
3. [Reading your ledger](03-reading-your-ledger.md) — see the whole trail of what
   your agent did, in one table.
4. [Declaring rules (no enforcement yet)](04-declaring-constraints.md) — write down
   the rules an action should follow, so a gateway can enforce them later without
   you changing any code.

## Wait — what's a capsule, in one breath?

Your agent did something (wrote a PO, sent an email, charged a card). A **capsule**
is a little sealed receipt of that action: *who* did it, *what* they did, and *what
happened* — hashed so it can't be quietly edited, and (optionally) logged to a public
list so anyone can later check it's real. You add **one line** where the action
happens; you get back proof you can hand to anyone.

That's the whole idea. The tutorials show you the line.
