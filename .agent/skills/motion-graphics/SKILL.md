---
name: motion-graphics
description: GPU-accelerated UI motion design, micro-interactions, choreographed page/tab transitions, and accessibility-first animation rules for Pure CSS3 + Google Lit web applications.
---

# OmniLearn Motion Design & UI Animation System (`SKILL.md`)

## 1. Core Motion Principles
- **Purposeful, Not Decorative:** Every animation must clarify spatial hierarchy, confirm user action, or mask network latency. Never animate static text blocks or decorative elements unnecessarily.
- **GPU-Accelerated Properties Only:** Animate strictly `transform` (`translate`, `scale`, `rotate`) and `opacity`. **NEVER** animate `width`, `height`, `top`, `left`, `margin`, or `padding` (prevents layout thrashing and maintains 60/120fps).
- **Mandatory Reduced Motion Support:** Always include a root `@media (prefers-reduced-motion: reduce)` override that collapses transition durations to `0.01ms` and disables continuous keyframe loops.

## 2. Motion Design Tokens (Add to `frontend/css/theme.css`)
Define and reuse these exact CSS custom properties across all stylesheets and Lit shadow/light DOM components:

```css
:root {
  /* Duration Scale */
  --duration-instant: 80ms;     /* Button press, checkbox/toggle feedback */
  --duration-fast: 160ms;       /* Hover states, tooltips, dropdowns */
  --duration-normal: 260ms;     /* Tab switches, card reveals, drawer slides */
  --duration-deliberate: 420ms; /* Modals, hero entrances, progress rings */

  /* Easing Curves */
  --ease-out-expo: cubic-bezier(0.16, 1, 0.3, 1);     /* Elements entering view (fast start, soft landing) */
  --ease-in-accel: cubic-bezier(0.4, 0, 1, 1);        /* Elements exiting view (quick departure) */
  --ease-in-out-smooth: cubic-bezier(0.4, 0, 0.2, 1); /* On-screen morphs & layout shifts */
  --ease-spring-snappy: cubic-bezier(0.34, 1.56, 0.64, 1); /* Badges, quiz pass checks, mastery milestones */
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

## 3. Component Choreography Rules

1. **Tab & View Transitions (`app.js`):**
- Incoming views fade in from `opacity: 0; transform: translateY(8px)` to `opacity: 1; transform: translateY(0)` over `var(--duration-normal)` using `var(--ease-out-expo)`.
- Directional navigation (e.g., Next/Previous quiz question or study day) slides horizontally (`translateX(16px)` forward, `translateX(-16px)` backward).

2. **Staggered Card Entrances (`notes-view.js`, `plan-view.js`, `sources-view.js`):**
- Apply sequential `animation-delay: calc(var(--stagger-index, 0) * 45ms)` capped at `360ms` maximum so lists cascade smoothly without making users wait.

3. **Micro-Interactions (`components.css`):**
- Primary buttons: subtle lift on hover (`translateY(-1px)`) over `var(--duration-fast)` and tactile press (`scale(0.97)`) on `:active` over `var(--duration-instant)`.
- Quiz answers & task checkboxes: use `var(--ease-spring-snappy)` for checkmark/badge scale-in (`scale(0.85) -> scale(1)`).

4. **Modals & Drawers (`download-modal.js`):**
- Backdrop fades `opacity: 0 -> 1` over `180ms`; modal dialog scales `scale(0.96) translateY(10px) -> scale(1) translateY(0)` over `var(--duration-normal)` with `var(--ease-out-expo)`.

5. **Skeleton Shimmers (Loading States):**
- During AI synthesis or search, render layout-matched skeleton blocks with a GPU-translated linear-gradient shimmer (`translateX(-100%) -> translateX(100%)`, `1.4s infinite ease-in-out`) instead of blank screens or blocking spinners.

## 4. Technical Guardrails for Lit Web Components
- Toggle state via CSS classes or `data-state` attributes—never overwrite `element.style.opacity` or `element.style.transform` directly in JS cleanup handlers.
- Never apply transitions to keyboard-shortcut command palettes or instant search typing inputs (must respond in `0ms`).
