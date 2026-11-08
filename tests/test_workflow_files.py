"""Tests for the custom agent files and the CLAUDE.md section. They read the real files in the repo.

Run with: /usr/bin/python3 -I tests/test_workflow_files.py
"""
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
AGENTS_DIR = os.path.join(ROOT, 'agents')

GUIDELINES = 'andrej-karpathy-skills:karpathy-guidelines'
TDD = 'test-driven-development'
REVIEWER_TOOLS = 'Edit, Write, NotebookEdit, Agent'

# Each agent's model, effort, disallowed tools and preloaded skills, as the README's model table sets them.
ROLES = {
    'implementer': ('opus', 'high', 'Agent', [GUIDELINES, TDD]),
    'implementer-copy': ('sonnet', 'medium', 'Agent', [GUIDELINES, TDD]),
    'implementer-escalated': ('fable', 'high', 'Agent', [GUIDELINES, TDD]),
    'plan-reviewer': ('fable', 'high', REVIEWER_TOOLS, [GUIDELINES]),
    'plan-simplifier': ('opus', 'high', REVIEWER_TOOLS, [GUIDELINES]),
    'task-reviewer': ('opus', 'high', REVIEWER_TOOLS, [GUIDELINES]),
    'fix-checker': ('sonnet', 'medium', REVIEWER_TOOLS, None),
    'final-reviewer': ('fable', 'high', REVIEWER_TOOLS, [GUIDELINES]),
}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def frontmatter(text):
    """Reads the simple YAML used here: `key: value` lines and `- item` lists under a key with no value."""
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('no frontmatter')
    end = lines.index('---', 1)
    fields, key = {}, None
    for line in lines[1:end]:
        stripped = line.strip()
        if stripped.startswith('- ') and key is not None:
            fields[key].append(stripped[2:].strip())
        elif ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key, value = key.strip(), value.strip()
            fields[key] = value if value else []
        elif stripped:
            raise ValueError('unexpected frontmatter line: ' + line)
    return fields, '\n'.join(lines[end + 1:])


class AgentFiles(unittest.TestCase):

    def test_every_agent_matches_the_role_table(self):
        files = sorted(f for f in os.listdir(AGENTS_DIR) if not f.startswith('.'))
        self.assertEqual(files, sorted(name + '.md' for name in ROLES))
        for name, (model, effort, disallowed, skills) in ROLES.items():
            with self.subTest(agent=name):
                fields, body = frontmatter(read(os.path.join('agents', name + '.md')))
                expected = {'name': name, 'model': model, 'effort': effort, 'disallowedTools': disallowed}
                if skills is not None:
                    expected['skills'] = skills
                description = fields.pop('description', '')
                self.assertIsInstance(description, str)
                self.assertTrue(description)
                self.assertEqual(fields, expected)
                self.assertTrue(body.strip())

    def test_section_stays_short_and_holds_the_exact_contract(self):
        text = read('claude-md/agentic-workflow.md')
        self.assertLessEqual(len(text.splitlines()), 30)  # About 25 lines.
        for must in ('failing-test-check.py', 'workflow-log.jsonl', 'workflow-report.py --brief',
                     'workflow-report.py --schema', 'Advisor: ', '**Failing-test check:** exempt:',
                     '/advisor fable', '/advisor off'):
            with self.subTest(must=must):
                self.assertIn(must, text)

    def test_section_tells_the_controller_how_to_run_the_failing_test_check(self):
        text = read('claude-md/agentic-workflow.md')
        for must in ('--link', 'node_modules', '--test-file', '--repo', '--timeout'):
            with self.subTest(must=must):
                self.assertIn(must, text)

    def test_section_defines_other_main_sessions(self):
        # The stage table's column needs a definition outside the table itself.
        text = read('claude-md/agentic-workflow.md')
        definitions = [line for line in text.splitlines() if 'Other main sessions' in line and not line.startswith('|')]
        self.assertEqual(len(definitions), 1)
        self.assertIn('Small path', definitions[0])

    def test_section_runs_scripts_with_the_system_python(self):
        # The scripts are installed without the executable bit, and the global constraint is /usr/bin/python3 -I.
        text = read('claude-md/agentic-workflow.md')
        self.assertIn('/usr/bin/python3 -I ~/.claude/scripts/failing-test-check.py', text)
        self.assertEqual(text.count('~/.claude/scripts/'), text.count('/usr/bin/python3 -I ~/.claude/scripts/'))

    def test_implementers_carry_the_five_rules(self):
        for name in ('implementer', 'implementer-copy', 'implementer-escalated'):
            _, body = frontmatter(read(os.path.join('agents', name + '.md')))
            for must in ('Simplest first', 'Reuse before writing', 'Match the codebase', 'No pattern yet',
                         '## Code conventions', 'parallel', 'fix: address review findings'):
                with self.subTest(agent=name, must=must):
                    self.assertIn(must, body)

    def test_reviewers_carry_the_finding_bar_and_reuse_rule(self):
        for name in ('task-reviewer', 'final-reviewer'):
            _, body = frontmatter(read(os.path.join('agents', name + '.md')))
            for must in ('shown path', 'risk area', 'at least Important', 'existing helper', '## Code conventions'):
                with self.subTest(agent=name, must=must):
                    self.assertIn(must, body)

    def test_fix_checker_judges_proportion_without_new_findings(self):
        _, body = frontmatter(read('agents/fix-checker.md'))
        for must in ('fixed, not proportionate', "Don't review anything else", 'parallel', 'only the tests'):
            self.assertIn(must, body)

    def test_plan_simplifier_checks_the_four_angles_without_cutting_requirements(self):
        _, body = frontmatter(read('agents/plan-simplifier.md'))
        for must in ('reuse', '## Code conventions', 'traces to', 'altitude', 'test proportion', 'risk area',
                     'spec requires', 'question for the owner', 'one pass', 'never consult the advisor'):
            with self.subTest(must=must):
                self.assertIn(must, body)

    def test_plan_reviewer_checks_nothing_required_was_cut_and_leaves_weight_to_the_simplifier(self):
        _, body = frontmatter(read('agents/plan-reviewer.md'))
        self.assertIn('nothing the spec requires was cut', body)
        self.assertNotIn('## Code conventions', body)
        self.assertNotIn("doesn't require, as a separate list", body)

    def test_section_holds_the_rules_and_monitoring_contract(self):
        text = read('claude-md/agentic-workflow.md')
        for must in ('Simplest first', 'Reuse before writing', 'Match the codebase', 'No pattern yet',
                     '## Code conventions', 'fixed, not proportionate', 'not found', 'restart',
                     'simplify.ran', 'fix_growth', 'WARN', 'workflow-report.py --health', 'plan-simplifier',
                     'at least Important'):
            with self.subTest(must=must):
                self.assertIn(must, text)


if __name__ == '__main__':
    unittest.main()
