# Presentation Speech — Botree Chat (Natural Language → SQL)

> A speaking script for presenting the project. Read it aloud, adapt the wording to your
> own voice, and pause where marked. Roughly **8–10 minutes**. Fill in the **[brackets]**.

---

## 0. Opening & self-introduction  *(~1 min)*

> "Good [morning/afternoon] everyone, and thank you for your time.

My name is **[your name]**, and I work as a **[your role]** at Botree. Over the past
**[few weeks]**, I've been building something I'm genuinely excited to show you — a project
called **Botree Chat**.

Let me start with the problem it solves. Today, when a sales manager or a business user
wants a number from our reporting system — 'what were my sales last month?', 'who are my top
distributors?' — they either dig through dashboards, or they wait on the data team to run a
query. That's slow, and it doesn't scale.

**What I built is a chatbot that lets anyone ask that question in plain English and get the
answer in seconds — safely, from our real database.**"

*(Pause. Let that land.)*

---

## 1. What it does — the simple version  *(~1 min)*

> "Here's the experience. You open a chat — it looks just like ChatGPT. You type:
> *'What are the total sales this year?'*

Behind the scenes, the system understands your question, writes the correct database query,
runs it, and shows you the answer — the written summary, the numbers in a table, and even the
exact query it used, if you want to see it.

You can ask follow-ups naturally — *'now break that down by distributor'* — and it remembers
the context. Every conversation is saved, so you can come back to it later.

And critically — **it only ever reads. It can never change or delete company data.** More on
that safety in a moment."

---

## 2. How it works — the algorithm  *(~2 min)*

> "Let me walk you through what happens when you ask a question. There are **six steps**, and
> the clever part is that **most of the time, we skip the expensive one.**

**Step one** — you ask in English.

**Step two** — we identify who you are and what you're allowed to see. This is your access
scope, and it's applied automatically.

**Step three** — and this is the key — we check: *have we seen this question before?* If yes,
we reuse the query we already have. **No AI needed. Zero cost.**

**Step four** — only if it's a genuinely new question do we call the AI model, which writes
the SQL query from your English.

**Step five** — before anything runs, the query passes through a safety gate. We check that
it's a read-only query, that it only touches approved tables, and we automatically inject your
access scope so you can't see beyond your territory.

**Step six** — we run it against the database and stream back the answer.

So the AI — the most expensive part — is used as little as possible. Repeated and reworded
questions are served from memory. That's the heart of the design."

*(Pause.)*

---

## 3. How we reduce tokens — the cost story  *(~2.5 min)*  ⭐ *the highlight*

> "Now let me talk about cost, because this is where the engineering really pays off.

AI models charge **per token** — roughly, per word they process. Every question you send
carries the whole database schema and a set of rules, so a single question can cost a couple
of thousand tokens. If a hundred people ask 'what are my sales this month' — that's the same
question a hundred times, and naively you'd pay for it a hundred times.

**We solved that with three layers of caching — three layers of memory.**

**Layer one — the exact-match cache.** If the same question is asked again — by anyone — we
recognise it instantly and reuse the query. **Zero AI tokens.**

**Layer two — the semantic cache.** This is the smart one. People phrase things differently —
'sales today' versus 'what did we sell today'. Layer two uses a small AI model **that runs on
our own servers** to understand that these two questions *mean the same thing*, and serves the
same cached query. Again — **zero tokens**, and because that matching model is local, it costs
us nothing per lookup. We also built in a safeguard so 'today' can never be confused with
'yesterday' — the savings never come from a wrong answer.

**Layer three — the result cache.** If the exact same query runs twice in a few minutes, we
even skip the database and return the stored result.

And there's a fourth trick: the queries we cache are stored **before** your personal access
filter is applied — so **one cached query serves everybody**, and each person's scope is added
on afterward. That's why one manager's question makes the next manager's question free.

*(Pause — then the proof.)*

**Here's the proof, measured on our real data.** We asked twelve questions, then asked them all
again. The first round cost about **twenty-two thousand tokens**. The second round —
**effectively zero**. That's a **91% reduction** in AI cost on repeat questions, and the answers
came back about **three times faster**. Every repeated question was served from memory at zero
cost.

*(Let the number sit.)* **Ninety-one percent.**"

---

## 4. Why it's robust and production-ready  *(~1.5 min)*

> "So it's cheap. But is it safe enough to put in front of the organisation? Yes — and this was
> designed in, not bolted on.

**First, it's read-only by design.** The safety gate inspects every single query and blocks
anything that isn't a pure read — no updates, no deletes, ever. Even if the AI were tricked into
writing a harmful query, it would be rejected before it reached the database.

**Second, access control.** A Sales Officer sees only their town; a Regional Manager sees only
their region; a VP sees everything. This is enforced by our own code — not by trusting the AI —
and it **fails closed**: if the system is ever unsure about your scope, it blocks the query
rather than risk showing you something you shouldn't see.

**Third, your data stays yours.** Only the *question* and the table names go to the AI — never
the actual sales figures. The results are computed in-house. The 'memory' model runs on our
servers. Nothing sensitive leaves.

**And it's tested.** We have **246 automated backend tests** and **10 full browser tests** that
walk through real login-to-answer journeys — all passing, all validated against our real
reporting database with over seven hundred thousand records. During that testing we found and
fixed six integration issues before they could ever reach a user.

**Almost the entire system is open-source** — no per-seat licensing, and it can run entirely on
company infrastructure. The only external piece is the AI model, and even that is swappable and
can be self-hosted."

---

## 5. Closing  *(~30 sec)*

> "To sum up: we've turned 'ask the data team and wait' into 'ask a question and get an answer.'
It's fast, it's safe, it respects who's allowed to see what, and it's engineered so we're never
paying the AI twice for the same question.

It's tested, it's documented, and it's ready for a controlled pilot.

Thank you — I'd be happy to take any questions, and I can show you a live demo right now."

*(Open for questions / switch to live demo.)*

---

## Appendix — quick answers for likely questions

- **"What technology is it built on?"** — Open-source throughout: React and Next.js on the
  front end; Python and FastAPI on the back end; PostgreSQL for storage. The AI model is
  Llama 3.1 today (low cost), and we can switch to Claude for higher accuracy with one setting.

- **"How much does it cost to run?"** — Almost nothing in licensing — everything is
  open-source. The only variable cost is the AI, and our caching cuts that by ~90%.

- **"Is our data safe / does it leave the company?"** — Only the question text and table names
  reach the AI; never the actual figures. Everything else stays on our infrastructure.

- **"What are the three cache layers again?"** — (1) exact-match: same question → reuse;
  (2) semantic: reworded question, matched by a local model → reuse; (3) result cache: same
  query within minutes → skip the database.

- **"How accurate is it?"** — On the free Llama model, occasionally a brand-new question
  produces a query that doesn't run — and it returns a polite 'no data' rather than a wrong
  number. A stronger model reduces this, and every successful question is cached, so it gets
  better over time.

- **"How long did it take?"** — [fill in], and it's fully documented with setup guides, a
  test report, and an architecture diagram.
</content>
