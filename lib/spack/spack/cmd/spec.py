# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import re
import sys

import spack
import spack.binary_distribution
import spack.cmd
import spack.config
import spack.environment
import spack.hash_types as ht
import spack.llnl.util.tty as tty
import spack.llnl.util.tty.color as color
import spack.package_base
import spack.solver.asp as asp
import spack.spec
from spack.cmd.common import arguments

description = "show what would be installed, given a spec"
section = "build"
level = "short"

#: output options
show_options = ("asp", "opt", "solutions")


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.epilog = """\
when an environment is active and no specs are provided, the environment root \
specs are used instead

for further documentation regarding the spec syntax, see:
    spack help --spec
"""
    arguments.add_common_arguments(subparser, ["long", "very_long", "namespaces"])

    install_status_group = subparser.add_mutually_exclusive_group()
    arguments.add_common_arguments(install_status_group, ["install_status", "no_install_status"])
    format_group = subparser.add_mutually_exclusive_group()
    format_group.add_argument(
        "-y",
        "--yaml",
        action="store_const",
        dest="format",
        default=None,
        const="yaml",
        help="print concrete spec as YAML",
    )
    format_group.add_argument(
        "-j",
        "--json",
        action="store_const",
        dest="format",
        default=None,
        const="json",
        help="print concrete spec as JSON",
    )
    format_group.add_argument(
        "--format",
        action="store",
        default=None,
        help="print concrete spec with the specified format string",
    )
    arguments.add_common_arguments(format_group, ["show_non_defaults"])

    subparser.add_argument(
        "-c",
        "--cover",
        action="store",
        default="nodes",
        choices=["nodes", "edges", "paths"],
        help="how extensively to traverse the DAG (default: nodes)",
    )
    subparser.add_argument(
        "-t", "--types", action="store_true", default=False, help="show dependency types"
    )
    arguments.add_common_arguments(subparser, ["specs"])
    arguments.add_concretizer_args(subparser)

    # debugging arguments
    subparser.add_argument(
        "--show",
        action="store",
        default="solutions",
        help="select outputs\n\ncomma-separated list of:\n"
        "  asp          asp program text\n"
        "  opt          optimization criteria for best model\n"
        "  output       raw clingo output\n"
        "  solutions    models found by asp program\n"
        "  all          all of the above",
    )
    subparser.add_argument(
        "--timers",
        action="store_true",
        default=False,
        help="print out timers for different solve phases",
    )
    subparser.add_argument(
        "--stats", action="store_true", default=False, help="print out statistics from clingo"
    )


def _process_result(result, show, required_format, kwargs):
    opt, _, _ = min(result.answers)
    if ("opt" in show) and (not required_format):
        tty.msg("Best of %d considered solutions." % result.nmodels)

        print()
        maxlen = max(len(s.name) for s in result.criteria)

        # Count how many times each (name, kind) pair appears to identify level-expanded criteria
        criterion_counts = {}
        for c in result.criteria:
            key = (c.name, c.kind)
            criterion_counts[key] = criterion_counts.get(key, 0) + 1

        # Organize sections by location inferred from priority ranges
        # Based on concretize.lp offsets
        pc = result.prio_constants

        def extract_level(priority, kind):
            """Extract the level from a priority value for level-expanded criteria.
            Note: fixed and level criteria share the same priority ranges, but
            fixed criteria are only active at level 0, while level criteria
            expand across all levels L0-L(max_depth-1).
            """
            # Level criteria can span from built_offset to built_offset + max_depth * level_opt
            level_upper_bound_built = pc.built_offset + pc.max_depth * pc.level_opt
            level_upper_bound_concr = pc.concr_offset + pc.max_depth * pc.level_opt

            if kind == asp.OptimizationKind.BUILD:
                # Check if in level range
                if pc.built_offset <= priority < level_upper_bound_built:
                    offset_from_level_start = priority - pc.built_offset
                    return offset_from_level_start // pc.level_opt
            elif kind == asp.OptimizationKind.CONCRETE:
                # Check if in level range
                if pc.concr_offset <= priority < level_upper_bound_concr:
                    offset_from_level_start = priority - pc.concr_offset
                    return offset_from_level_start // pc.level_opt
            return None

        sections = [
            ("High Priority", lambda c: c.priority >= pc.high_offset),
            ("Build Priority", lambda c: pc.built_offset <= c.priority < pc.high_offset),
            ("Hinge Priority", lambda c: pc.hinge_offset <= c.priority < pc.built_offset),
            ("Concrete/Reuse Priority", lambda c: pc.concr_offset <= c.priority < pc.hinge_offset),
            ("Low Priority", lambda c: pc.low_offset <= c.priority < pc.concr_offset),
        ]

        for section_name, section_filter in sections:
            # Collect all criteria in this section
            section_criteria = []
            for criterion in result.criteria:
                if section_filter(criterion):
                    section_criteria.append(criterion)

            if not section_criteria:
                continue

            color.cprint(f"\n@*{{{section_name}:}}")

            # Group criteria by (name, kind) for horizontal layout
            criteria_groups = {}
            for criterion in section_criteria:
                key = (criterion.name, criterion.kind)
                if key not in criteria_groups:
                    criteria_groups[key] = []
                criteria_groups[key].append(criterion)

            # Sort groups by highest priority in each group
            sorted_groups = sorted(criteria_groups.items(),
                                   key=lambda x: max(c.priority for c in x[1]),
                                   reverse=True)

            # Prepare rows data
            rows = []
            for key, group in sorted_groups:
                name, kind = key
                # Sort criteria in group by priority (highest first)
                group_sorted = sorted(group, key=lambda c: c.priority, reverse=True)

                # Get the highest priority for this group
                highest_priority = group_sorted[0].priority

                # Track previous value for de-accumulation
                prev_value = 0
                values_by_level = {}

                # Collect values by level
                for criterion in group_sorted:
                    if len(group) > 1:  # Level-expanded
                        internal_level = extract_level(criterion.priority, criterion.kind)
                        if internal_level is not None:
                            display_value = criterion.value - prev_value
                            prev_value = criterion.value
                            # Reverse level for display: internal max_depth-1 (roots) -> display L0
                            display_level = pc.max_depth - 1 - internal_level
                            values_by_level[display_level] = display_value
                    else:  # Fixed criterion - always show in L0
                        values_by_level[0] = criterion.value

                rows.append((highest_priority, values_by_level, name, kind))

            # Build and print header (always use leveled format)
            # Display levels in reverse order: L0 = roots (internally max_depth-1)
            header_cols = ["Priority"] + [f"L{i}" for i in range(pc.max_depth)] + ["Criterion"]
            col_widths = [8] + [6] * pc.max_depth + [maxlen]

            # Right-align all headers except last (Criterion)
            header_parts = [f"{col:>{w}}" for col, w in zip(header_cols[:-1], col_widths[:-1])]
            header_parts.append(f"{header_cols[-1]:<{col_widths[-1]}}")
            header = "  " + "  ".join(header_parts)
            color.cprint("@*{" + header + "}")

            # Print rows
            for priority, values_by_level, name, kind in rows:
                # Build value columns
                value_cols = []
                for level in range(pc.max_depth):
                    if level in values_by_level:
                        value_cols.append((values_by_level[level], kind))
                    else:
                        value_cols.append((None, kind))

                # Format the row (right-align all values)
                row_parts = [f"  @K{{{priority:>8}}}"]

                for value, k in value_cols:
                    if value is None:
                        row_parts.append(f"  @K{{{'-':>6}}}")
                    elif value > 0:
                        if k == asp.OptimizationKind.CONCRETE:
                            row_parts.append(f"  @b{{{value:>6}}}")
                        elif k == asp.OptimizationKind.BUILD:
                            row_parts.append(f"  @g{{{value:>6}}}")
                        else:
                            row_parts.append(f"  @y{{{value:>6}}}")
                    else:
                        row_parts.append(f"  @K{{{value:>6}}}")

                # Determine color for criterion name based on if any value > 0
                lc = "@K"
                if any(v > 0 for v in values_by_level.values() if v is not None):
                    if kind == asp.OptimizationKind.CONCRETE:
                        lc = "@b"
                    elif kind == asp.OptimizationKind.BUILD:
                        lc = "@g"
                    else:
                        lc = "@y"

                row_parts.append(f"  {lc}{{{name:<{maxlen}}}}")
                color.cprint("".join(row_parts))

        print()
        print()
        color.cprint("  @*{Legend:}")
        color.cprint("    @g{Specs to be built}")
        color.cprint("    @b{Reused specs}")
        color.cprint("    @y{Other criteria}")
        print()

    # dump the solutions as concretized specs
    if "solutions" in show:
        if required_format:
            for spec in result.specs:
                # With -y, just print YAML to output.
                if required_format == "yaml":
                    # use write because to_yaml already has a newline.
                    sys.stdout.write(spec.to_yaml(hash=ht.dag_hash))
                elif required_format == "json":
                    sys.stdout.write(spec.to_json(hash=ht.dag_hash))
                else:
                    print(spec.format(required_format))
        else:
            tree_str = spack.spec.tree(result.specs, color=sys.stdout.isatty(), **kwargs)
            sys.stdout.write(tree_str)
        print()

    if result.unsolved_specs and "solutions" in show:
        tty.msg(asp.Result.format_unsolved(result.unsolved_specs))


def spec(parser, args):
    # these are the same options as `spack spec`
    fmt = spack.spec.DISPLAY_FORMAT
    if args.namespaces:
        fmt = "{namespace}." + fmt

    show_status = args.install_status
    if show_status:
        spack.binary_distribution.load_buildcache_index()
        status_fn = spack.cmd.buildcache_status_fn(spack.binary_distribution.BINARY_INDEX)
    else:
        status_fn = None

    kwargs = {
        "cover": args.cover,
        "format": fmt,
        "hashlen": None if args.very_long else 7,
        "show_types": args.types,
        "status_fn": status_fn,
        "hashes": args.long or args.very_long,
        "highlight_version_fn": (
            spack.package_base.non_preferred_version if args.non_defaults else None
        ),
        "highlight_variant_fn": (
            spack.package_base.non_default_variant if args.non_defaults else None
        ),
    }

    # process output options
    show = re.split(r"\s*,\s*", args.show)
    if "all" in show:
        show = show_options
    for d in show:
        if d not in show_options:
            raise ValueError(
                "Invalid option for '--show': '%s'\nchoose from: (%s)"
                % (d, ", ".join(show_options + ("all",)))
            )

    # Format required for the output (JSON, YAML or None)
    required_format = args.format

    # If we have an active environment, pick the specs from there
    env = spack.environment.active_environment()
    if args.specs:
        specs = spack.cmd.parse_specs(args.specs)
    elif env:
        specs = list(env.user_specs)
    else:
        args.subparser.error("requires at least one spec or an active environment")

    solver = asp.Solver()
    output = sys.stdout if "asp" in show else None
    setup_only = set(show) == {"asp"}
    unify = spack.config.get("concretizer:unify")
    allow_deprecated = spack.config.get("config:deprecated", False)
    if unify == "when_possible":
        for idx, result in enumerate(
            solver.solve_in_rounds(
                specs,
                out=output,
                timers=args.timers,
                stats=args.stats,
                allow_deprecated=allow_deprecated,
            )
        ):
            if "solutions" in show:
                tty.msg("ROUND {0}".format(idx))
                tty.msg("")
            else:
                print("% END ROUND {0}\n".format(idx))
            if not setup_only:
                _process_result(result, show, required_format, kwargs)
    elif unify:
        # set up solver parameters
        # Note: reuse and other concretizer prefs are passed as configuration
        result = solver.solve(
            specs,
            out=output,
            timers=args.timers,
            stats=args.stats,
            setup_only=setup_only,
            allow_deprecated=allow_deprecated,
        )
        if not setup_only:
            _process_result(result, show, required_format, kwargs)
    else:
        for spec in specs:
            result = solver.solve(
                [spec],
                out=output,
                timers=args.timers,
                stats=args.stats,
                setup_only=setup_only,
                allow_deprecated=allow_deprecated,
            )
            if not setup_only:
                _process_result(result, show, required_format, kwargs)
