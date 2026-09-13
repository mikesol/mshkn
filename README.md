# mshkn

[Uncle Bob said](https://x.com/unclebobmartin/status/2098432570887217520):

> I've spend the last several weeks working on a harness that tightly constrains the agents to work the way that I want them to work.  I set up all kinds of gates, and tests, and tools, and protocols, and ...

> And while I was heads-down getting that to work, the agents got a LOT better.  So much so that when I came up for air, the need for my harness was obviated.  Indeed, the need for _any_ but the most liberal of harnesses may be obviated.

> What does this mean going forward?  I'm not sure.  But I'm beginning to think that harnesses should not treat agents as components within a software design.

You're right, Uncle Bob. _Agents_ should treat _harnesses_ as components within a software design.

That's `mshkn`. It is an experiment in agents that create their own harnesses, and treat their harness as one of several considerations as they busy themselves accomplishing various tasks, from managing calendars to compiling reports to coding.

`mshkn` is split into three parts:

1. A small disposable computer framework. Computers' dependencies are stated declaratively, they start in a couple hundred milliseconds, and they can be forked and merged with reckless abandon.

2. A minimum viable agent, projected onto disposable computers, that I call the embryo. This is the smallest possible agent that can grow into something useful.

3. A collection of capabilities that our little agent acquires through discourse. Security, web search, coding, personality, and many other things are capabilities. The agent acquires them by discussing with its operator.

But first, why am I doing this?

# Agents for fun and profit, but mostly fun

I've built ~10 production agents for various companies, using harnesses like Openclaw, Hermes, Grok Bot, and my own `cc-disco`. You sort of get the same thing every time — a super capable back-office worker with many different faces (Discord, countless _ad hoc_ dashboards, an email account) that acts as a lubricant and accelerant for the stuff you need to get done on a given day.

At the same time, you have a gaggle of companies in YC and beyond trying to solve the "personal agent" problem, meaning an agent that _feels_ more like yours and _gets_ you. Some are reinventing the whole model training stack to solve this problem. Others are operating at the harness level. This project explores the latter.

My belief is that current open-weight models like Qwen 3.8 Flash are plenty smart to get anyone, and the only thing you need to do is, as Uncle Bob suggests, get out of the agent's way and let it do its thing.

This project exists to explore that design space. By giving an agent quick access to whatever compute it needs for the specific task it's doing, you give it lots of time to try, fail, learn, and eventually grow. And by baking next-to-no assumptions into its initial harness, it isn't laden with a ton of assumptions that constrain its functionality.

# Ok cool but like how do I try this

XXX

By that point, you should have an agent that's built its own harness. What you do after that is up to you. You can try giving it some new capacities:

- security
- personality
- chat
- digital detox
- web search
- coding
- UI-making

Or you can give it your own home-grown capacities.

# Disposable computers

> This whole thing is quite experimental. If you're looking for a production-grade disposable computer service, check out [fly.io sprites](https://sprites.dev). I liberally borrowed from many of its ideas while prompting `mshkn` into existence.

Disposable computers are super important for harnessless-agents. Without them, agents will accrete dependencies, jobs, and processes on one big computer, which works until it doesn't. Eventually, they'll OOM or have a dependency clash, and precious tokens will go into fighting against their environment.

A disposable computer is a manifest declaring what software it ships with (think Docker), frozen memory and disk, and the ability to fork and merge really, really quickly. Armed with this, the intrepid agent that, for example, needs to mux videos, can reach for a disposable computer to do just that and nothing else.

To be fair, these computers don't really _need_ to be disposable. But once you've accumulated 10k computers and don't know which ones you can or can't turn off, you'll be pretty happy that everything is disposable.

There are other nice accoutrements that come from my functional programming bona fides that I'm pretty sure agents will love (at least so far they seem to). XXX

As I build this library out, I'm constantly pushing the minimum viable computer down along multiple axes. Less time, less stuff inside of it, less state, less cost, less complexity. These are all things that an agent should discover, remember, and checkpoint. So while it's not strictly necessary to co-develop harnessless agents and disposable computers, I find that they are the yin and yang of the same cosmic universe of personalized agents.

A nice way to ease into the VM part of `mshkn` is XXX. I'm not gonna waste time documenting the architecture, your LLM will do that for you. So just point it to XXX and it'll give you the summary of your dreams.

# Embryo

The embryo is the minimum viable agent that can grow into any other agent. I think of it as a collection of orthogonal agentic "axioms" that are irreduceable and cannot be induced by an external prompter. Broadly, this goes into two categories:

- **boostrapping**: the ability to call an LLM. This requires a pretty limited set of features: XXX.
- **silent failures**: things it can never figure out through trial and error. This is also quite small: XXX.

Both targets are not fixed and I'm constantly trying to reduce the embryo more and more. Again, it's unclear if starting from a small embryo is the best way to create an agent, but I like hanging out at the far-end of a conceptual continuum, if for nothing else than so people can say "hey, let's not do what that guy is doing and instead try something more moderate."

# Capabilities

This is where the rubber hits the road. Agents need to do stuff like ordering movie tickets, finding a trustworthy babysitter, and psychotherapy. Super importantly, they also need to _not do_ stuff so that they don't become dopamine-spike-inducing automatons that lull us into blue-pilling life. The latter is perhaps the most-important thing your agent can do and the one that most harnesses, especially for-profit ones, don't really think about.

The first capability _any_ agent has is _hatching_. After that, the capabilities in this repo are organized as a sort of DAG so that you can lead your agent through its learning journey.

All capabilities in this repo have been documented to work automatically at least once. That is, from a fixed script in XXX, they have made it to the end and acquired the capability. That being said, these are not deterministic systems, and a lot of times they will fail to grasp instructions and require human interaction to guide them. And of course, not all capabilities will suit you. You may want striciter or more lax security, you may have strong opinions about design guiding UI elaboration, and for personality, you'll need to completely customize the capability to _you_ and the type of agent _you'd_ like to interact with day-in-day-out for the next _n_ years. Think of each capability as a white-glove prompting environment that allows you to follow a pretty reliable script to get a decent result, but is counting on you to add your own flair to get a superb result.

# Hacking?

This repo is massively, unapologetically agentically coded. Basically the only thing that an agent hasn't spit out is this README.md, and even there, I had to show it to agents numerous times until they stroked my ego enough that I was ready to release it to the world at large. So first and foremost, please point Astra or Fable at this repo for best results!

Next, the main directions of research I'm always pursuing are:
- faster VMs along all p95s that count (XXX...)
- cleaner functional patterns for the entire VM lifecycle (XXX...)
- the most minimal embryo possible without tacit knowledge threaded through hidden channels (XXX...)
- capabilities (XXX...)

I basically only ever hack on this when I have tokens to spare. If you find the idea interesting and would like to contribute, please do!