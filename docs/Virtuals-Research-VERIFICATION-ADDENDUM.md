# Verification addendum to `Virtuals-vs-Tilla-Research.md`

*A re-verification pass (2026‑07‑22). The report's core thesis holds — Virtuals is agent-to-agent-first with a buyer-side human concierge (Butler), not a seller-owned one-prompt storefront, and its commerce uses the same escrow/evaluator/reputation shape Tilla runs. Below are the corrections and one significant update. **Code claims (about Tilla) are code-verified; external claims (about Virtuals) are from a web-research pass and should get a direct source-check before going in the pitch — competitor facts move fast and a wrong one is a credibility risk.***

## Corrections to the report's own claims

1. **The ~80/20 escrow split — downgrade the confidence.** The report tags "80% Provider / 20% protocol" as *3‑0 verified*. Re-check: the 80/20 wording exists in **historically-indexed** whitepaper content, but the cited pages now 404 and the **current** live docs specify no split. Treat it as *"historically documented, since removed from live docs; current docs specify no split,"* not *3‑0 verified*. Also delete the "evaluator gets a slice of the ~20%" inference (§4 lesson 5) — the 20% was described as going to the **protocol**, not the evaluator.

2. **The khala.io traction numbers — don't blanket-refute all of them.** The report says to discard 30.8M Butler conversations, 2.1M jobs, $3.7M revenue, $473M through Butler. Nuance: the khala source is uncited (distrust is fair), **but "~2.1M completed jobs" and "~$3.7M agent revenue" track Virtuals' own ecosystem-dashboard trajectory** as reported by independents. → Cite those two **with attribution to the dashboard (not khala) and the caveat that they are ecosystem-wide** (including bot/swap volume), and keep discarding the Butler-conversation and "$473M through Butler" figures.

3. **Revenue Network is shipped, not roadmap.** §6's caveat lists "Revenue Network" as staged/roadmap, but the report's own addendum quotes its **launch** press release (for the $1M/month figure) — internally inconsistent. Per the research pass, the Virtuals Revenue Network **launched Feb 12, 2026** → move it to "shipped."

## Significant update since the doc was written

4. **Robinhood Chain integration (early July 2026).** The research pass reports that Robinhood Chain integrated Virtuals' agent infrastructure at launch, with Virtuals crossing ~$150M agent volume there within days, plus tokenized-index products, peaqOS/robotics ACP buying, and a CCIP migration — and that the "aGDP" subsidy program was **wound down at ~$4M cumulative agent revenue**. → If confirmed, this *strengthens* Tilla's framing: Virtuals keeps expanding its **chain footprint and speculation surface**, still without shipping a seller-owned one-prompt storefront — and even ended the seller subsidy, reinforcing "agent commerce isn't yet organically self-sustaining." **Verify the Robinhood/subsidy-wind-down claims against primary sources before using them in the submission.**

## What still holds (no change)

- The headline positioning line in the addendum ("even the largest agent platform subsidizes agent commerce; its human surface is a buyer-side concierge; Tilla is the seller-owned one-prompt storefront that sells to humans and agents on OKX rails") **survives** — it's the strongest, most defensible framing and is submission-ready.
- The ACP SDK state machine (`open→budget_set→funded→submitted→completed`), role-gating, `AssetToken.usdc(chainId)`, and reputation-sorted registry are the right things to mine; **reputation-ranked discovery** remains the single best low-effort adopt-item for Tilla (verified absent from our discovery output today).
- Do **not** copy the bonding-curve token launchpad.
