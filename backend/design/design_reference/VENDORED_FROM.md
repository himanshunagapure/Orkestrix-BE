Source: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
Tag: v2.11.0
Commit: 6142b07
License: MIT (see LICENSE in this folder)
Vendored: 2026-07-29
Files taken: data/{products,styles,colors,typography,charts,ux-guidelines,ui-reasoning}.csv,
             data/stacks/angular.csv, scripts/{core.py,design_system.py}
Not taken: scripts/search.py, templates/, skill.json, all other 21 stack files
Integration: backend/services/design_reference/ (resolve_theme_preferences → DesignSystemGenerator.generate)
To update: re-run the sparse-checkout clone at a new tag, diff CSVs before replacing,
           re-test design_system.generate_design_system() / DesignSystemGenerator.generate()
           call signature hasn't changed.
