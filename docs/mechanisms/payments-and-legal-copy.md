# Payments and legal copy for Paddle review (issue #574)

**Status: purchase and refund implemented in #578; subscription operations and lifecycle in #595/#596; frontend and public billing copy in #597.**

## Purchase and refund flow (#578)

Profile obtains configured packs and the public client token from the authenticated
checkout-config endpoint. Paddle.js previews local prices and opens a one-page
overlay. Clicking Buy disables every Buy button until `checkout.loaded`,
`checkout.closed`, or `checkout.error`, or until 15 seconds pass with none of
those events. `checkout.completed` closes the overlay on the same page (no
`successUrl` redirect). The block then shows a pending notice and polls
`GET /payments/purchases/{transaction_id}` immediately and every 3 seconds.
That read is authenticated and read-only: the signed-in user's own ledger row
returns `credited: true` and the two-decimal credit amount; an unknown
transaction or another user's transaction returns `credited: false` and
`credits: null`. A credited result shows the amount and `router.refresh()`
reloads the server-rendered balance. Polling stops 120 seconds after
`checkout.completed` if the purchase is still uncredited. Only a signed
`transaction.completed` webhook grants purchased credits to the cash bucket.
Ops refunds debit unused purchased cash credits within 120 days, then
create a proportional Paddle adjustment in the same request; a failed Paddle
call rolls the debit back. A rejected adjustment restores the debit once.
External adjustments alert Ops without changing the ledger. Monthly plan-fee
deduction and renewal are implemented in #595/#596. A repeated Buy click on the same pack, within
the same page visit, reopens the still-unfinished transaction from the
`checkout.loaded` event instead of creating a second one (issue #592): the
component remembers the price id and transaction id carried by the last
`checkout.loaded` event in a ref (not persisted anywhere), and reuses that
transaction id on the next Buy for the same pack. `checkout.completed`,
`checkout.error`, and the existing 15-second open timeout clear the memory,
so the next Buy opens a fresh transaction. A Buy for a different pack also
opens a fresh transaction but leaves the memory in place until that
checkout's own `checkout.loaded` replaces it.
This file holds the English source copy and the change map. The zh-Hans and
zh-Hant translations ship in `frontend/src/locales/`; their reviewed source
lives in the owner's project notes.

## Why this exists

Portfonia will take payment through Paddle as Merchant of Record (MoR):
Paddle is the seller of record, so buyers see Paddle, not the operator, on
checkout and card statements. Paddle's domain review requires a product
description, pricing, key features, Terms / Refund Policy / Privacy Policy
reachable from navigation, and the seller's name in the Terms. Paddle's
Acceptable Use Policy prohibits investment or financial advice and
payment/money-transfer services, so the copy keeps the existing
"intelligence, not advice" boundary explicit and never presents credits as
a cash-redeemable balance. Full rationale and sources: issue #574
(Reasons, Exploration).

## Confirmed decisions (owner, 2026-09-28)

- Seller name on the site is the brand **Portfonia** only. "Portfonia LLC"
  was a placeholder (no entity exists) and "Portfonia AI" was an
  inconsistency; both are removed. The operator's legal name goes only to
  Paddle.
- Plans: **Weekly briefing 0.99 credits/month**, **Mon/Wed/Fri briefing
  1.99 credits/month** (matching `users.report_cadence` `weekly`/`mwf`).
- Credit packs: **US$10 = 10 credits**, **US$20 = 20 credits**. One credit
  is one US dollar (`docs/mechanisms/credit-ledger.md`).
- Plan fees are deducted monthly from the credit balance, complimentary
  credits first, then purchased credits (matches `consume_credits`).
- Refunds: unused purchased credits, within 120 days of that purchase,
  capped at that purchase's amount, to the original payment method.
  Complimentary, promotional, and referral credits (the `gift` bucket and
  `invite_rebate`) are not refundable. 120 days is Paddle's card-refund
  limit.
- Credits are "prepaid service credits, usable only for Portfonia
  subscriptions, non-transferable, with no cash value except as provided
  in the Refund Policy".
- Governing law is neutral (operator's place of establishment, mandatory
  consumer protection preserved, informal resolution first). No named
  jurisdiction.
- The U.S. tax-resident restriction (Terms §4) is removed.
- Service status is "limited public service", replacing "closed beta" /
  "invite-gated".
- The two plans differ only in briefing cadence; everything else is
  included in both.
- Low balance: users are emailed a reminder in advance when their balance
  will not cover the next monthly fee.
- Plan change or cancellation (2026-09-28): the already-charged fee for the
  current month was to be returned pro rata for the unused part. **The cancellation
  portion was superseded on 2026-09-30 (#597): cancellation runs through the
  paid period with no return. Plan changes still return the unused part pro rata.**
- Credits do not expire. After a purchase's 120-day refund window there is
  no other refund channel; the credits stay usable.
- Account deletion removes holdings, investment-style settings, and other
  personal data; credit and transaction records are kept, no longer linked
  to the email address. The copy says "removed", not "permanently
  removed".
- If the balance is still short at renewal, the plan stops and the plan's
  features become unavailable; today that means scheduled briefings stop.
- A pro-rata return goes back to the bucket(s) the fee was deducted from
  (`gift` or `cash`), so returned complimentary credits stay
  non-refundable. How a fee deducted across two or more buckets is
  returned is an implementation question, bound by this principle.

- The operator's legal name and phone number are given to a payment
  provider only in its back office, never displayed on the site. If a
  provider requires public display of either, that provider (Paddle, Creem,
  or any similar channel) is abandoned rather than the site changed. The
  Paddle seller handbook asks for the seller's legal name in the Terms and a
  support email and phone on the site; the site currently shows the brand
  name and email only.

No open owner decisions remain for this copy.

## Copy conventions

- US dollar amounts appear only on Pricing, Refund Policy, and Terms
  "Payments and Credits". Everywhere else the product says "credits" only.
- The Paddle reseller notice appears on Pricing, Refund Policy, and Terms.
- Shipped locale strings never name the provider inside document text.
  General mentions ("processed by Paddle", "Paddle's Buyer Terms") use the
  `{merchantOfRecord}` placeholder, filled by `MERCHANT_OF_RECORD` (currently
  `Paddle`) in `frontend/src/app/_components/legal-document.tsx`. The
  reseller notice is provider-mandated verbatim wording (Paddle seller
  handbook: "Our order process is conducted by our online reseller
  Paddle.com. Paddle.com is the Merchant of Record for all our orders. Paddle
  provides all customer service inquiries and handles returns."), so it lives
  whole in each locale's `legal.resellerNotice` and is inserted at
  `{resellerNotice}`. Switching to another Merchant of Record (for example
  Creem) means changing the constant and rewriting `legal.resellerNotice` in
  the three locales. A non-MoR processor (such as PayPal Business) is not a
  drop-in swap: the operator becomes the seller, which changes tax, refund,
  and legal copy.
- Placeholders are filled only by `LegalDocument`; document trees are read
  with `t.raw()` and must never pass through next-intl `t()`, which would
  parse them as ICU arguments.
- `lastUpdated` is set to the ship date by the implementation issue.
- New documents reuse the existing `legal.<doc>` shape:
  `title`, `lastUpdated`, `intro`, `sections[{heading, body[]}]`.

## Change map

| Locale key | Change |
|---|---|
| `legal.nav.pricing`, `legal.nav.refund` | new: "Pricing", "Refund Policy" |
| `legal.pricing` | new document (below) |
| `legal.refund` | new document (below) |
| `legal.terms` | revised document (below); old §4 removed, sections renumbered |
| `legal.privacy` | revised document (below) |
| `legal.terms.sections[3].body[1..4]` | #597: change/cancel rules, ET daily limit, unsubscribe consequences, verified-only notices |
| `legal.pricing.sections[3].body[2]` | #597: changes return pro rata; cancellation has no return |
| `legal.refund.sections[1].body[1]` | #597: cancellation does not return remaining-day credits |
| `legal.{terms,pricing,refund}.lastUpdated` | #597: 2026-09-30 |
| `home.hero.eyebrow` | "MVP · Multi-user closed beta" → "Limited public service" |
| `home.preview.footnote` | "Actual content varies depending on your subscription tier." → "Briefing frequency depends on your plan." |
| `home` FAQ "What does it cost?" answer | closed-beta text → see Home copy below |
| `home.status` | "MVP — multi-user closed beta. …" → see Home copy below |

Navigation (implementation issue): Pricing and Refund Policy links are
added wherever Terms and Privacy are linked today (home footer, Profile,
`LegalDocument` cross-links).

## Pricing (`legal.pricing`)

- **title**: Pricing
- **intro**: Portfonia is a portfolio intelligence service. You choose a
  briefing plan and pay for it with prepaid service credits.

1. **Plans**
   - Weekly briefing — 0.99 credits per month. One personalized briefing
     email per week, tied to the holdings you have entered.
   - Mon / Wed / Fri briefing — 1.99 credits per month. Three personalized
     briefing emails per week (Monday, Wednesday, Friday).
   - The two plans differ only in how often briefings are sent.
2. **Included in every plan**
   - Holdings upload from CSV, Excel, or Markdown, with row-level editing.
   - Portfolio overview: allocation, performance against benchmarks, and
     historical volatility and risk views.
   - Briefings covering price anomalies, technical position, macro and
     company news mapped to your holdings, and a forward calendar of macro
     and earnings events, each causal claim with a confidence label.
   - Briefings in English or Chinese.
3. **Credits**
   - Credits are sold in packs: US$10 for 10 credits, or US$20 for 20
     credits. Prices are in US dollars; any sales tax or VAT is calculated
     at checkout.
   - Credits are prepaid service credits, usable only for Portfonia
     subscriptions. They are non-transferable and have no cash value
     except as provided in the Refund Policy.
4. **How billing works**
   - Your plan fee is deducted from your credit balance at the start of
     each monthly billing period. Complimentary credits are used first,
     then purchased credits.
   - If your balance will not cover the next month's fee, we email you a
     reminder in advance. If the balance is still not enough at renewal,
     your plan stops and its features become unavailable — currently, your
     scheduled briefings stop.
   - If you change your plan, the unused part of the current month's fee is returned to your credit balance pro rata and the new plan starts that day. If you cancel, your plan stays active until the end of the period already paid and nothing is returned.
   - Credits do not expire.
5. **Refunds**
   - Unused purchased credits can be refunded within 120 days of purchase.
     See the Refund Policy for details.
6. **Payment processing**
   - Our order process is conducted by our online reseller Paddle.com.
     Paddle.com is the Merchant of Record for all our orders. Paddle
     provides all customer service inquiries and handles returns.
7. **Not investment advice**
   - Portfonia is an information service. It does not provide investment,
     legal, or tax advice, and does not tell you what to buy, sell, or
     hold.

## Refund Policy (`legal.refund`)

- **title**: Refund Policy
- **intro**: This policy explains when credits purchased for Portfonia can
  be refunded.

1. **Refundable credits**
   - Credits you purchased through checkout that have not yet been used
     can be refunded within 120 days of the purchase. A refund is limited
     to the amount paid for that purchase and is returned to the original
     payment method.
2. **Non-refundable credits**
   - Complimentary, promotional, and referral credits are not refundable.
   - Credits already applied to a billing period are not refunded to your payment method. When you change a plan, the unused part of the current month's fee is returned to your credit balance pro rata. When you cancel, nothing is returned for the remaining days.
   - After 120 days from a purchase, credits from that purchase can no
     longer be refunded. They do not expire and remain usable for your
     subscription.
3. **Statutory rights**
   - This policy does not limit any right of withdrawal or refund you have
     under Paddle's Buyer Terms or the consumer-protection law of the
     country where you live.
4. **How to request a refund**
   - Email info@portfonia.com from your account email, or contact Paddle
     through the link in your purchase receipt. Approved refunds are
     processed by Paddle; card refunds usually appear within 3–5 business
     days.
5. **Payment processing**
   - Our order process is conducted by our online reseller Paddle.com.
     Paddle.com is the Merchant of Record for all our orders. Paddle
     provides all customer service inquiries and handles returns.
6. **Nature of credits**
   - Credits are prepaid service credits, usable only for Portfonia
     subscriptions. They are non-transferable and have no cash value
     except as provided in this policy.

## Terms of Service (`legal.terms`)

Unchanged sections are marked *(unchanged)*; the implementation keeps the
existing text for them verbatim.

- **title**: Terms of Service
- **intro**: These Terms of Service govern your use of Portfonia. Please
  read them together with our Privacy Policy and Refund Policy.

1. **Acceptance of Terms** — "Portfonia", "we", and "us" refer to the
   operator of the Portfonia service. By creating an account or using
   Portfonia ("the Service"), you agree to these Terms of Service. If you
   do not agree, do not use the Service.
2. **What Portfonia Is (and Is Not)** *(unchanged)*
3. **Eligibility and Accounts**
   - You must be able to form a binding contract to use the Service. You
     are responsible for keeping your login credentials confidential and
     for all activity under your account.
   - The Service is offered as a limited public service: sign-up may be
     limited to people who have joined the waitlist or received an
     invitation, and features may change as the Service develops.
4. **Payments and Credits** (new)
   - Paid plans are billed in prepaid service credits. Credits are sold in
     packs priced in US dollars (1 credit = US$1 at purchase); current
     plans and packs are listed on the Pricing page.
   - Your plan fee is deducted from your credit balance at the start of each monthly billing period, complimentary credits first. If you change your plan, the unused part of the current month's fee is returned to your credit balance pro rata and the new plan starts that day. If you cancel, your plan stays active until the end of the period already paid and nothing is returned.
   - You can adjust your subscription (subscribe, change plan, cancel, or resume) once per day, Eastern Time.
   - If you unsubscribe an address from report email and no verified address remains on your account, your subscription is treated as cancelled: it stays active until the end of the period already paid, nothing is returned, and it does not resume when you verify an address again.
   - Balance reminders and expiry notices are sent only to a verified address.
   - Credits do not expire. They are usable only for Portfonia
     subscriptions, are non-transferable, and have no cash value except as
     provided in the Refund Policy.
   - Our order process is conducted by our online reseller Paddle.com.
     Paddle.com is the Merchant of Record for all our orders. Paddle
     provides all customer service inquiries and handles returns. Your
     purchase is also subject to Paddle's Buyer Terms.
5. **Your Holdings Data** *(unchanged; was §5)*
6. **Acceptable Use** *(unchanged; was §6)*
7. **Intellectual Property** — Portfonia and its original content,
   features, and functionality are owned by Portfonia. Reports generated
   for your account are yours to use personally; they are not licensed for
   redistribution as a data feed or third-party product.
8. **Disclaimers** *(unchanged)*
9. **Limitation of Liability** — To the fullest extent permitted by law,
   Portfonia is not liable for any indirect, incidental, or consequential
   damages, or for any financial loss arising from your use of, or reliance
   on, the Service. Nothing in these Terms limits liability that cannot be
   limited under applicable law.
10. **Termination** — You may stop using the Service and request deletion
    of your account and data at any time. Portfonia may suspend or
    terminate accounts that violate these Terms. Refunds on termination
    follow the Refund Policy.
11. **Changes to These Terms** *(unchanged)*
12. **Governing Law and Disputes**
    - These Terms are governed by the laws of the place where the operator
      of Portfonia is established, without regard to conflict-of-law
      principles. This does not deprive you of the protection of mandatory
      consumer-protection laws of the country where you live.
    - If a dispute arises, please contact us first at info@portfonia.com so
      we can try to resolve it informally and in good faith.
13. **Contact** *(unchanged)*

Removed: old §4 "Regional Restriction" (U.S. tax residents).

## Privacy Policy (`legal.privacy`)

1. **Overview** *(unchanged)*
2. **Information We Collect** — existing three paragraphs unchanged, plus:
   - Payment and credit information: purchases are processed by Paddle.com
     as Merchant of Record. Paddle collects your payment details and
     billing information under its own privacy notice; we receive
     transaction records such as order ID, amount, currency, country, and
     the email used at checkout, but never your full card number. We also
     keep a record of your credit balance and every credit added or used.
3. **How We Use Your Information** — existing paragraph unchanged, plus:
   "We use payment and credit records to provide your plan, process
   refunds, and meet accounting and tax obligations."
4. **Data Storage and Security** *(unchanged)*
5. **Third-Party Processing** — existing LLM paragraphs unchanged, plus:
   "Payments are processed by Paddle.com, which acts as Merchant of Record
   and handles your payment information under its own privacy notice."
   Heading changes from "Third-Party Processing (LLMs)" to "Third-Party
   Processing".
6. **Data Retention** — existing paragraph kept except its last sentence,
   which becomes "You may request deletion of your account at any time (see
   "Your Rights" below)." (previously "full deletion of your account and
   associated data", which contradicted the retained credit records), plus: "When your
   account is deleted, your holdings, investment-style settings, reports,
   and other personal data are removed. Credit and transaction records
   are kept, no longer linked to your email address, for accounting and
   tax purposes."
7. **Your Rights** — "You can review and update your holdings and profile
   information directly in the Service. You can request deletion
   of your account by contacting us; deletion removes your holdings,
   investment-style settings, and authentication account as described in
   Data Retention."
8. **Cookies and Sessions** *(unchanged)*
9. **Children's Privacy** *(unchanged)*
10. **Applicable Law** — "We handle personal data in accordance with the
    data-protection laws that apply to us, including those of the country
    where you live where they apply." (Replaces the target-regions /
    "under legal review" text.)
11. **Changes to This Policy** *(unchanged)*
12. **Contact** *(unchanged)*

The retention text in §6/§7 reflects existing behavior:
`app/services/user_purge.py` deletes holdings, accounts, reports, upload
jobs, `user_investment_context`, and email verifications, then the `users`
row that holds the email; `credit_ledger` rows stay keyed by `user_id` with
`user_deleted_at` set (`docs/mechanisms/credit-ledger.md`). The copy says
"removed" rather than "permanently removed" (owner decision), since
database backups keep a 30-day retention
(`docs/mechanisms/backup-and-ops.md`).

Left to the implementation issue: the pro-rata return algorithm when one
fee spans several buckets, and how returned purchased credits map to a
purchase's 120-day refund window.

## Home copy

- `home.hero.eyebrow`: Limited public service
- `home.preview.footnote`: Briefing frequency depends on your plan.
- FAQ "What does it cost?": Plans start at 0.99 credits per month for a
  weekly briefing, or 1.99 credits per month for Monday / Wednesday /
  Friday briefings. Credits are sold in US$10 and US$20 packs — see
  Pricing.
- `home.status`: Limited public service. Portfonia maps market context
  back to your real holdings so you can make timely, well-informed
  decisions. AI-generated content, for information only — not investment
  advice.

## Paddle submission checklist (owner, after implementation ships)

- Site live on HTTPS with Pricing, Refund Policy, Terms, Privacy in the
  footer navigation.
- Seller name "Portfonia" in the Terms matches the brand entered in the
  Paddle application; the operator's legal name is entered in the
  application only.
- Sign-up is limited, so prepare an invite link for the Paddle reviewer in
  case a test account is requested.
