// Master-resume layout template — mirrors data/input/master_resume.md structure.

#let cfg = json("data.json")
#let layout = cfg.layout
#let sz(v) = v * 1pt

#set page(
  margin: (top: 0.5in, bottom: 0.5in, left: 0.6in, right: 0.6in),
  paper: "us-letter",
)
#set text(font: layout.font, size: sz(layout.font_size))
#set par(leading: 0.55em, justify: false)

#align(center)[
  #text(weight: "bold", size: sz(layout.name_size))[#cfg.name] \
  #text(size: sz(layout.headline_size))[#cfg.headline] \
  #text(size: sz(layout.contact_size))[#cfg.contact_line]
]

#v(4pt)
#line(length: 100%, stroke: 0.5pt + gray)
#v(2pt)

#let section(title, body) = [
  #text(weight: "bold", size: sz(layout.section_heading_size))[#title]
  #v(-2pt)
  #line(length: 100%, stroke: 0.3pt + gray)
  #v(2pt)
  #body
  #v(4pt)
]

#for sec in cfg.sections {
  if sec.type == "summary" [
    #section(sec.title, [
      #if sec.summary != "" [ #text(size: sz(10.5))[#sec.summary] ]
      #if sec.skills_line != "" [
        #v(3pt)
        #text(size: sz(10), weight: "medium")[#sec.skills_line]
      ]
    ])
  ] else if sec.type == "skills" [
    #section(sec.title, text(size: sz(10.5))[#sec.skills.join(" · ")])
  ] else if sec.type == "experience" [
    #section(sec.title, [
      #for e in sec.entries [
        #grid(columns: (1fr, auto), gutter: 4pt,
          text(weight: "bold")[#e.company #h(4pt) | #h(4pt) #emph(e.title)],
          text(size: sz(9.5))[#e.dates],
        )
        #if e.location != "" [
          #text(size: sz(9.5), fill: gray)[#e.location]
        ]
        #v(-2pt)
        #for b in e.bullets [ - #b ]
        #v(3pt)
      ]
    ])
  ] else if sec.type == "education" [
    #section(sec.title, [
      #for e in sec.entries [
        #text(weight: "bold")[#e.school #h(4pt) | #h(4pt) #emph(e.degree) #h(4pt) | #h(4pt) #e.dates]
        #v(2pt)
      ]
    ])
  ] else if sec.type == "bullets" [
    #section(sec.title, [
      #for b in sec.bullets [ - #b ]
    ])
  ] else if sec.type == "projects" [
    #section(sec.title, [
      #for e in sec.entries [
        #text(weight: "bold")[#e.title]
        #v(-2pt)
        #for b in e.bullets [ - #b ]
        #v(3pt)
      ]
    ])
  ]
}
