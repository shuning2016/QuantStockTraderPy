---
name: code-review
description: Review recent code changes in your project using the code-reviewer agent. Use this skill whenever you want feedback on your code before committing, to evaluate code quality, spot potential bugs, check adherence to best practices, or get suggestions for improvement. Invoke it to analyze your recent changes and provide structured, actionable feedback with specific file references and severity levels.
---

# Code Review Skill

## Overview

This skill leverages Claude's specialized code-reviewer agent to analyze recent code changes in your project and provide constructive, detailed feedback on code quality, potential issues, and opportunities for improvement.

## How It Works

When you invoke this skill:

1. Claude checks your git status and recent commits to understand what code has changed
2. Launches the code-reviewer agent with context about those changes
3. Requests comprehensive feedback on quality, bugs, performance, best practices, and test coverage
4. Generates a structured markdown report with file paths and actionable recommendations

## Instructions

Follow these steps to review code:

1. **Check git history**
   ```bash
   git status
   git log --oneline -10
   git diff HEAD~5..HEAD
   ```

2. **Launch the code-reviewer agent** and request a review of:
   - Files that were changed
   - What the changes do (from commit messages and diffs)
   - Context about the project (Python backend, Flask app, etc.)

3. **Ask for feedback on:**
   - Code quality and readability (naming, complexity, clarity)
   - Potential bugs or edge cases
   - Performance or efficiency concerns
   - Best practices and standards adherence
   - Test coverage for new/modified code
   - Security considerations

4. **Format the output** as a markdown report with:
   - Clear section headers
   - Specific file paths and line references
   - Severity levels for issues (🔴 Critical, 🟠 Major, 🟡 Minor)
   - Actionable recommendations
   - Examples where helpful

## What to Review

The reviewer should look for:

- **Code Quality** — Naming clarity, function/method complexity, logical structure
- **Correctness** — Potential bugs, edge cases, unhandled scenarios  
- **Performance** — Efficiency concerns, unnecessary operations, algorithmic improvements
- **Best Practices** — Standards adherence, design patterns, error handling
- **Testing** — Coverage for new functionality, edge case testing, integration testing
- **Security** — Input validation, data protection, authentication/authorization
- **Maintainability** — Documentation, modularity, future-proofing

## Tips for Best Results

- Provide context about what you've been working on
- Ensure your code is in a reviewable state (staged, committed, or in a clean branch)
- Clear commit messages help the reviewer understand the "why" behind changes
- The skill works best when there are recent commits or active changes to analyze
