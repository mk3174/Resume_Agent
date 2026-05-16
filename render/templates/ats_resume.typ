// ATS-clean single-column resume template.
// Rendered by render/typst.py. All content is injected as Typst variables.

#let cfg = json("data.json")

#set page(
  margin: (top: 0.5in, bottom: 0.5in, left: 0.6in, right: 0.6in),
  paper: "us-letter",
)
#set text(font: "Helvetica", size: 10.5pt)
#set par(leading: 0.55em, justify: false)

// --- Header ---
#align(center)[
  #text(weight: "bold", size: 18pt)[#cfg.name] \
  #text(size: 11pt)[#cfg.headline] \
  #text(size: 9pt)[
    #cfg.contact_line
  ]
]

#v(4pt)
#line(length: 100%, stroke: 0.5pt + gray)
#v(2pt)

// --- Section helper ---
#let section(title, body) = [
  #text(weight: "bold", size: 11pt, upper(title))
  #v(-2pt)
  #line(length: 100%, stroke: 0.3pt + gray)
  #v(2pt)
  #body
  #v(4pt)
]

// --- Summary ---
#if cfg.summary != "" [
  #section("Summary", text(size: 10.5pt)[#cfg.summary])
]

// --- Skills ---
#if cfg.skills.len() > 0 [
  #section("Skills", text(size: 10.5pt)[#cfg.skills.join(" \u{00B7} ")])
]

// --- Experience ---
#if cfg.experience.len() > 0 [
  #section("Experience", [
    #for e in cfg.experience [
      #grid(columns: (1fr, auto), gutter: 4pt,
        text(weight: "bold")[#e.company #h(4pt) -- #h(4pt) #emph(e.title)],
        text(size: 9.5pt)[#e.dates],
      )
      #v(-2pt)
      #for b in e.bullets [
        - #b
      ]
      #v(3pt)
    ]
  ])
]

// --- Selected Projects ---
#if cfg.project_bullets.len() > 0 [
  #section("Selected Projects", [
    #for b in cfg.project_bullets [
      - #b
    ]
  ])
]

// --- Education ---
#if cfg.education.len() > 0 [
  #section("Education", [
    #for e in cfg.education [
      #grid(columns: (1fr, auto), gutter: 4pt,
        text(weight: "bold")[#e.school],
        text(size: 9.5pt)[#e.dates],
      )
      #text(size: 10pt, emph(e.degree))
      #v(2pt)
    ]
  ])
]
