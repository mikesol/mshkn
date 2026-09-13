# mshkn

[Uncle Bob said](https://x.com/unclebobmartin/status/2098432570887217520):

> I've spent the last several weeks working on a harness that tightly constrains the agents to work the way that I want them to work.  I set up all kinds of gates, and tests, and tools, and protocols, and ...

> And while I was heads-down getting that to work, the agents got a LOT better.  So much so that when I came up for air, the need for my harness was obviated.  Indeed, the need for _any_ but the most liberal of harnesses may be obviated.

> What does this mean going forward?  I'm not sure.  But I'm beginning to think that harnesses should not treat agents as components within a software design.

You're right, Uncle Bob. _Agents_ should treat _harnesses_ as components within a software design.

That's `mshkn`. It is an experiment in agents that create their own harnesses, and treat their harness as one of several considerations as they busy themselves accomplishing various tasks, from managing calendars to compiling reports to coding.

`mshkn` is split into three parts:

1. A small disposable computer framework. Computers' dependencies are stated declaratively, they start in under a second from a warm snapshot, and they can be forked and merged with reckless abandon.

2. A minimum viable agent, projected onto disposable computers, that I call the embryo. This is the smallest possible agent that can grow into something useful.

3. A collection of capabilities that our little agent acquires through discourse. Security, web search, coding, personality, and many other things are capabilities. The agent acquires them by discussing with its operator.

But first, why am I doing this?

# Agents for fun and profit, but mostly fun

I've built ~10 production agents for various companies, using harnesses like [OpenClaw](https://github.com/openclaw/openclaw), [Hermes](https://github.com/NousResearch/hermes-agent), [Grok Bot](https://x.ai/bot), and my own [`cc-disco`](https://github.com/mikesol/cc-disco). You sort of get the same thing every time — a super capable back-office worker with many different faces (Discord, countless _ad hoc_ dashboards, an email account) that acts as a lubricant and accelerant for the stuff you need to get done on a given day.

At the same time, you have a gaggle of companies in YC and beyond trying to solve the "personal agent" problem, meaning an agent that _feels_ more like yours and _gets_ you. Some are reinventing the whole model training stack to solve this problem. Others are operating at the harness level. This project explores the latter.

My belief is that current open-weight models like Qwen 3.8 Flash are plenty smart to get anyone, and the only thing you need to do is, as Uncle Bob suggests, get out of the agent's way and let it do its thing.

As my friend Claude Opus 5 would say, "Get out of the way" is ["load-bearing"](https://news.ycombinator.com/item?id=48905248) in my previous sentence, so let me be precise about it. The embryo's harness is strict about one thing: _permission_. It starts being able to do nothing, not knowing anyone, and with the sole ability to propose stuff that its assumedly-human overlord approves. What it can do, however, is fail at stuff, and basically, it fails its way to success. Like Myst and Monkey Island, it slowly grows its inventory through discovery, which unblocks it in successive challenges until it can basically do anything.

It's a fun design space, no? By giving an agent quick access to whatever compute it needs for the specific task it's doing, you give it lots of time to try, fail, learn, and eventually grow. And by baking next-to-nothing into its initial harness, it isn't laden with a ton of assumptions that constrain its functionality.

# Ok cool but like how do I try this

First, the cost, because it isn't zero. `mshkn` runs on one bare-metal Linux box with KVM (a rented Hetzner-class machine is fine; you can mosey on over to `docs/infrastructure.md` to see the minimum spec), plus an R2 bucket for checkpoints and a wildcard domain for the computers' routes. Setting the box up is `DEPLOY.md`, executed verbatim. Growing an agent costs model tokens: a hatch run is roughly thirty model calls, a few dollars and twenty minutes, and you'll do it more than once. If that sounds like a lot for a personal agent, it is, and pushing every one of those numbers down is most of what I work on.

Then, once the box is up:

```bash
# on your machine, in a checkout of this repo
uv sync
python -m mshkn accounts create --help          # mint an account and a key on the host, then:
cat > .env <<'ENV'
MSHKN_API_URL=https://api.<your-domain>
MSHKN_API_KEY=<the key>
ANTHROPIC_API_KEY=<yours>
OPENAI_API_KEY=<yours, for embeddings>
ENV

uv run capability run hatch --approve ask --keep # speak the first script to a fresh embryo; you approve each proposal
uv run capability promote hatch docs/embryo/hatch/<date>-run-<n>   # keep the brain that made it through
uv run capability run security --approve ask --keep                # the next script starts from the promoted brain
```

Each run writes its transcript, every command, the final state and a verdict under `docs/embryo/<capability>/`, so you can read exactly what your agent did and why the judge scored it the way it did. `embryo/README.md` has the curl for speaking through each door by hand when you'd rather not use the driver.

By that point, you should have an agent that's built its own harness. What you do after that is up to you. You can try giving it some new capabilities:

- security
- personality
- chat
- digital detox
- web search
- coding
- UI-making

Or you can give it your own home-grown capabilities, which you can and should upstream to this repo!

# Disposable computers

> This whole thing is quite experimental. If you're looking for a production-grade disposable computer service, check out [fly.io sprites](https://sprites.dev). I liberally borrowed from many of its ideas while prompting `mshkn` into existence.

Disposable computers are super important for harnessless agents. Without them, agents will accrete dependencies, jobs, and processes on one big computer, which works until it doesn't. Eventually, they'll OOM or have a dependency clash, and precious tokens will go into fighting against their environment.

A disposable computer is a manifest declaring what software it ships with (think Docker), frozen memory and disk, and the ability to fork and merge really, really quickly. Armed with this, the intrepid agent that, for example, needs to mux videos, can reach for a disposable computer to do just that and nothing else.

To be fair, these computers don't really _need_ to be disposable. But once you've accumulated 10k computers and don't know which ones you can or can't turn off, you'll be pretty happy that everything is disposable.

There are other nice accoutrements that come from my functional programming bona fides that I'm pretty sure agents will love (at least so far they seem to). A checkpoint is immutable, named, and has a parent, so the history of a computer is a DAG you can point at. A computer is the evaluation of that value, and evaluating it is cheap enough that the agent never keeps one alive while it thinks: a turn is a fork, a checkpoint and a destroy, and the next turn forks again. Forking is O(1) in the size of the state, so a fork of a 50 MB working set costs what a fork of 1 MB does. Two forks of the same parent can be merged three-ways back into one checkpoint. A labelled chain can be forked exclusively, so two callers arriving together admit exactly one and the other waits or fails, which is how a stateful verb gets a fold over its invocations without a lock in sight (it's kinda of like linear programming, but with computers as the base unit instead of functions!). None of this is novel to anyone who has used git or a persistent data structure. It's just my spin on it, and it's open source, which means perhaps you'll find it useful as well.

As I build this library out, I'm constantly pushing the minimum viable computer down along multiple axes. Less time, less stuff inside of it, less state, less cost, less complexity. These are all things that an agent should discover, remember, and checkpoint. So while it's not strictly necessary to co-develop harnessless agents and disposable computers, I find that they are the yin and yang of the same cosmic universe of personalized agents.

A nice way to ease into the VM part of `mshkn` is the end-to-end suite under `tests/e2e/`: it reads as a tour, one phase per idea, and it is the definition of done for the project, so if a test says a thing happens, it actually happens on a live host. I'm not gonna waste time documenting the architecture, your LLM will do that for you. So just point it to `docs/ARCHITECTURE.md` and `docs/what-exists.md` and it'll give you the summary of your dreams.

# Embryo

The embryo is the minimum viable agent that can grow into any other agent. I think of it as a collection of orthogonal agentic "axioms" that are irreducible and cannot be induced by an external prompter. Broadly, this goes into two categories:

- **bootstrapping**: what cannot be learned because learning is predicated on it. To wit:

  - That `propose` exists, and that a human called root approves or rejects what is proposed. 
  - That a turn is a life: every message is one turn, at the end of which the state is checkpointed and the computer on which the turn happens is destroyed, so memory is the disk and nothing else.
  - That all effects happen outside the agent, through verbs, each on its own computer.
  
- **silent failures**: things it can never figure out through trial and error. This is also quite small.

   - That every value it passes to a verb is shell-quoted for it.
   - That anonymous input is never remembered, whatever it asks.
   - That a `chain` verb's disk persists on its own checkpoint chain while an `ephemeral` one's does not. That root sees a proposal the moment it is made and it cannot be edited afterwards. Each of these would otherwise be learned by a run that quietly did the wrong thing and never found out.

Everything else the agent arrives at itself, either because a script asks for it as an outcome, or because it tries something and meets a refusal that names what would have been valid. The whole seed for hatching is `embryo/seed.md`.

Bootstrapping and silent failures are moving targets, and I'm constantly trying to reduce the embryo more and more. Again, it's unclear if starting from a small embryo is the best way to create an agent, but I like hanging out at the far-end of a conceptual continuum, if for nothing else than so people can say "hey, let's not do what that guy is doing and instead try something more moderate."

# Capabilities

This is where the rubber hits the road. Agents need to do stuff like ordering movie tickets, finding a trustworthy babysitter, and psychotherapy. Super importantly, they also need to _not do_ stuff so that they don't become dopamine-spike-inducing automatons that lull us into blue-pilling life. The latter is perhaps the most-important thing your agent can do and the one that most harnesses, especially for-profit ones, don't really think about.

The first capability _any_ agent has is _hatching_. After that, the capabilities in this repo are organized as a sort of DAG so that you can lead your agent through its learning journey.

A capability is a markdown file under `embryo/capabilities/`: a few lines of frontmatter naming what it depends on and which postconditions judge it, then a table of turns. Each turn is fixed words spoken through a named door (root's authenticated one, or the public one signed or unsigned) and the outcome those words are asking for. One driver, `uv run capability run <name>`, speaks the rows to a real model, approves or asks you to approve what the agent proposes, waits for builds, offers a repair phrase when something fails, and judges the postconditions at the end. A run that reaches every postcondition can be promoted, which pins its brain and its verbs under fixed labels so the next capability in the DAG starts from that state rather than from an egg. `docs/embryo/` holds every run, passing or not, with a round table per capability saying what each one reached, what it cost and why it stopped where it did.

All capabilities in this repo have been documented to work automatically at least once. That is, from a fixed script in `embryo/capabilities/`, driven by `uv run capability run`, they have made it to the end and acquired the capability. That being said, these are not deterministic systems, and a lot of times they will fail to grasp instructions and require human interaction to guide them. And of course, not all capabilities will suit you. You may want stricter or more lax security, you may have strong opinions about design guiding UI elaboration, and for personality, you'll need to completely customize the capability to _you_ and the type of agent _you'd_ like to interact with day-in-day-out for the next _n_ years. Think of each capability as a white-glove prompting environment that allows you to follow a pretty reliable script to get a decent result, but is counting on you to add your own flair to get a superb result.

# Hacking?

This repo is massively, unapologetically agentically coded. Basically the only thing that an agent hasn't spit out is this README.md, and even there, I had to show it to agents numerous times until they stroked my ego enough that I was ready to release it to the world at large. So first and foremost, please point Astra or Fable at this repo for best results!

Next, the main directions of research I'm always pursuing are:
- faster VMs along all p95s that count (the E2E suite pins them: a warm fork or restore under 650 ms, a cold create under 1.6 s, a checkpoint around a second; every one of those is a number I want to halve, and the tests fail if a change moves them the wrong way)
- cleaner functional patterns for the entire VM lifecycle (checkpoints as values and computers as their evaluation is most of the way there; what's still ad hoc is merge, which is a filesystem three-way today and should understand more than files, and the relay, which is how a turn becomes a chain of forks with no computer alive in between)
- the most minimal embryo possible without tacit knowledge threaded through hidden channels (the seed is a page; every round of the measure in `docs/embryo/` tries to shorten it and records what breaks, and the rule that a line must be either bootstrap or silent failure is what keeps it from growing back)
- capabilities (security is the next one after hatch, and the DAG after that is whatever the list above turns into once an agent has actually walked it; the open question is how much of each script survives contact with a model that isn't the one it was written against)

I basically only ever hack on this when I have tokens to spare. If you find the idea interesting and would like to contribute, please do! `CLAUDE.md` is the working agreement: the gate, the live E2E expectation, and no merges without a human saying so.

# License

MIT
