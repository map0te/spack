"""Trail visualizer propagator for debugging Spack's concretization process."""
from collections import defaultdict
from typing import Dict, List, Set

import spack.llnl.util.tty as tty


class TrailVisualizer:
    """Custom propagator to visualize the solving trail.

    Samples the trail during solving to provide insight into how Clingo
    explores the search space and makes decisions.
    """

    def __init__(self, max_samples: int = 50, sample_interval: int = 100, verbose: bool = False):
        """
        Args:
            max_samples: Maximum number of trail snapshots to keep
            sample_interval: Sample every N propagations
            verbose: If True, print samples during solving
        """
        self.max_samples = max_samples
        self.sample_interval = sample_interval
        self.verbose = verbose
        self.propagation_count = 0
        self.samples: List[Dict] = []
        self.symbol_map: Dict[int, str] = {}
        self.decision_count = 0
        self.conflict_count = 0

    def init(self, init):
        """Called once before solving starts."""
        # Build a map from literal to symbolic representation
        for atom in init.symbolic_atoms:
            lit = init.solver_literal(atom.literal)
            self.symbol_map[lit] = str(atom.symbol)
        return True

    def propagate(self, control, changes):
        """Called when the solver propagates assignments."""
        self.propagation_count += 1

        # Sample periodically
        if self.propagation_count % self.sample_interval == 0:
            self._take_sample(control)

        return None  # No conflicts

    def undo(self, thread_id, assign, changes):
        """Called when the solver backtracks."""
        self.conflict_count += 1

    def check(self, control):
        """Called when a model is found or on total assignment."""
        self._take_sample(control, label="MODEL")
        return None

    def _take_sample(self, control, label=None):
        """Take a snapshot of the current trail."""
        assignment = control.assignment

        # Collect current trail info
        trail_info = {
            'propagation': self.propagation_count,
            'decision_level': assignment.decision_level,
            'label': label,
            'decisions': [],
            'implied': [],
            'atoms': []
        }

        # Iterate through assigned literals
        for lit, symbol_str in self.symbol_map.items():
            try:
                if assignment.is_true(lit):
                    trail_info['atoms'].append((symbol_str, True))

                    # Try to determine if it's a decision
                    level = assignment.level(lit)
                    if level > 0:
                        try:
                            decision = assignment.decision(level)
                            if decision == lit:
                                trail_info['decisions'].append(symbol_str)
                            else:
                                trail_info['implied'].append(symbol_str)
                        except:
                            trail_info['implied'].append(symbol_str)
                    else:
                        trail_info['implied'].append(symbol_str)

                elif assignment.is_false(lit):
                    trail_info['atoms'].append((symbol_str, False))
            except:
                # Skip literals that cause issues
                continue

        self.samples.append(trail_info)

        # Keep only recent samples
        if len(self.samples) > self.max_samples:
            self.samples.pop(0)

        if self.verbose and label:
            tty.debug(f"Trail sample {len(self.samples)}: {label}, level={trail_info['decision_level']}")

    def print_summary(self):
        """Print a summary of the solving process."""
        tty.msg("Trail Visualizer Summary")
        tty.msg(f"Total propagations: {self.propagation_count}")
        tty.msg(f"Conflicts (backtracks): {self.conflict_count}")
        tty.msg(f"Samples taken: {len(self.samples)}\n")

        if not self.samples:
            tty.msg("No samples collected.")
            return

        tty.msg("Recent Trail Snapshots (last 5):")
        tty.msg("-" * 70)

        for i, sample in enumerate(self.samples[-5:], 1):
            label = f" [{sample['label']}]" if sample['label'] else ""
            tty.msg(f"\nSnapshot {i}{label}:")
            tty.msg(f"  Propagation: {sample['propagation']}")
            tty.msg(f"  Decision level: {sample['decision_level']}")
            tty.msg(f"  Assigned atoms: {len(sample['atoms'])}")

            if sample['decisions']:
                tty.msg(f"  Decisions ({len(sample['decisions'])}):")
                for dec in sample['decisions'][:3]:
                    tty.msg(f"    → {self._format_atom(dec)}")
                if len(sample['decisions']) > 3:
                    tty.msg(f"    ... and {len(sample['decisions']) - 3} more")

            if sample['implied']:
                tty.msg(f"  Implied ({len(sample['implied'])}):")
                for imp in sample['implied'][:5]:
                    tty.msg(f"    ⇒ {self._format_atom(imp)}")
                if len(sample['implied']) > 5:
                    tty.msg(f"    ... and {len(sample['implied']) - 5} more")

        tty.msg("\n" + "=" * 70)

    def _format_atom(self, atom_str: str) -> str:
        """Format atom string for better readability."""
        # Shorten long atoms
        if len(atom_str) > 60:
            return atom_str[:57] + "..."
        return atom_str

    def print_dag_summary(self):
        """Print a light DAG representation of dependencies found."""
        tty.msg("\nDependency Graph Summary:")
        tty.msg("-" * 70)

        # Extract dependency relationships from sampled atoms
        deps = defaultdict(set)
        packages = set()

        for sample in self.samples:
            for atom in sample['decisions'] + sample['implied']:
                if 'depends_on' in atom:
                    # Parse depends_on(parent, child, deptype)
                    try:
                        parts = atom.split('(')[1].split(')')[0].split(',')
                        if len(parts) >= 2:
                            parent = parts[0].strip().strip('"')
                            child = parts[1].strip().strip('"')
                            packages.add(parent)
                            packages.add(child)
                            deps[parent].add(child)
                    except:
                        pass

        if not deps:
            tty.msg("  No dependency information captured in samples")
            return

        # Print simple tree
        printed = set()
        for pkg in sorted(packages):
            if pkg not in deps or pkg not in printed:
                self._print_tree(pkg, deps, printed, indent=0)

        tty.msg("")

    def _print_tree(self, pkg: str, deps: Dict, printed: Set, indent: int):
        """Recursively print dependency tree."""
        if pkg in printed:
            return

        prefix = "  " * indent + ("├── " if indent > 0 else "")
        tty.msg(f"{prefix}{pkg}")
        printed.add(pkg)

        if pkg in deps:
            for child in sorted(deps[pkg]):
                if child not in printed:
                    self._print_tree(child, deps, printed, indent + 1)
