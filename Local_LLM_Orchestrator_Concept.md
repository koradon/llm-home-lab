# Local LLM Orchestrator Concept (Claude Code replacement)

## Goal

Build a local middleware layer between an agent (for example OpenCode) and
multiple local LLMs running on different machines.

Assumptions:

-   The agent sees **one OpenAI-compatible API**.
-   Multiple LM Studio instances can run behind that API.
-   Models are **stateless** and do not keep session memory.
-   The orchestrator stores all session and execution state.

------------------------------------------------------------------------

# Proposed architecture

``` text
                OpenCode

                    │

        http://llm.home:8080/v1

                    │

        Local LLM Orchestrator

                    │

      ┌─────────────┼─────────────┐
      │             │             │
  LM Studio      LM Studio    LM Studio
   MacBook        Windows      Linux
```

The orchestrator selects a model based on:

-   availability,
-   load,
-   context size,
-   model specialization,
-   response time.

------------------------------------------------------------------------

# Session Manager

The most important component.

It stores:

-   conversation history,
-   summaries of older messages,
-   current task,
-   workspace state,
-   tool results,
-   RAG context.

The model never stores memory.

------------------------------------------------------------------------

# Workspace State

Includes:

-   current Git branch,
-   git diff,
-   open files,
-   unsaved changes,
-   test results,
-   logs,
-   repository index.

This enables model switching without losing context.

------------------------------------------------------------------------

# Tool State

Tool state should be independent of the currently selected model.

Examples:

-   filesystem
-   terminal
-   git
-   docker
-   kubectl
-   PostgreSQL
-   MCP

The model only invokes tools.

------------------------------------------------------------------------

# Problems to solve

## 1. Different models

Qwen may make different decisions than DeepSeek.

Potential solutions:

-   keep one preferred model per session,
-   pass an execution plan between turns,
-   persist architectural decisions.

------------------------------------------------------------------------

## 2. Reasoning continuity

Models do not transfer their internal reasoning to each other.

What can be persisted:

-   plan,
-   TODO list,
-   decisions,
-   constraints.

------------------------------------------------------------------------

## 3. Cache

When switching models, context often needs to be re-sent.

Possible improvements:

-   prompt cache,
-   embeddings,
-   smart summarization,
-   selective context retrieval.

------------------------------------------------------------------------

## 4. Terminal

Terminal has its own state:

-   working directory,
-   active environment,
-   environment variables,
-   running processes.

The orchestrator should manage terminal sessions explicitly.

------------------------------------------------------------------------

# Routing

Example routing rules:

-   coder-large -> Qwen
-   reviewer -> DeepSeek
-   embeddings -> small model
-   planner -> largest model

------------------------------------------------------------------------

# Future extensions

-   task queues,
-   load balancing,
-   health checks,
-   monitoring,
-   model statistics,
-   automatic failover,
-   distributed cache,
-   agent task scheduler.

------------------------------------------------------------------------

# Long-term vision

Build something like "Kubernetes for home LLM infrastructure."

The agent does not know where the model runs.

It only sees:

    http://llm.home:8080/v1

All intelligence lives in the orchestrator:

-   session management,
-   model routing,
-   memory,
-   tools,
-   RAG,
-   cache,
-   machine switching,
-   multi-host local LLM management.
