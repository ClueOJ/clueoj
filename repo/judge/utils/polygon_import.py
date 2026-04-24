import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from operator import itemgetter

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone
from lxml import etree as ET

from judge.models import Language, Problem, ProblemData, ProblemGroup, ProblemTestCase, ProblemTranslation, \
    ProblemType, Profile, Solution
from judge.utils.problem_data import ProblemDataCompiler
from judge.utils.zipfiles import open_zipfile_for_write
from judge.views.widgets import django_uploader

PANDOC_FILTER = r"""
local function normalize_quote(text)
    -- These four quotes are disallowed characters.
    -- See DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS
    text = text:gsub('\u{2018}', "'") -- left single quote
    text = text:gsub('\u{2019}', "'") -- right single quote
    text = text:gsub('\u{201C}', '"') -- left double quote
    text = text:gsub('\u{201D}', '"') -- right double quote
    return text
end

local function escape_html_content(text)
    -- Escape HTML/Markdown/MathJax syntax characters
    text = text:gsub('&', '&amp;') -- must be first
    text = text:gsub('<', "&lt;")
    text = text:gsub('>', "&gt;")
    text = text:gsub('*', '\\*')
    text = text:gsub('_', '\\_')
    text = text:gsub('%$', '<span>%$</span>')
    text = text:gsub('~', '<span>~</span>')
    return text
end

function Math(m)
    -- Fix math delimiters
    local delimiter = m.mathtype == 'InlineMath' and '~' or '$$'
    return pandoc.RawInline('html', delimiter .. m.text .. delimiter)
end

function Image(el)
    -- And blank lines before and after the image for caption to work
    return {pandoc.RawInline('markdown', '\n\n'), el, pandoc.RawInline('markdown', '\n\n')}
end

function Code(el)
    -- Normalize quotes and render similar to Codeforces
    local text = normalize_quote(el.text)
    text = escape_html_content(text)
    return pandoc.RawInline('html', '<span style="font-family: courier new,monospace;">' .. text .. '</span>')
end

function CodeBlock(el)
    -- Normalize quotes
    el.text = normalize_quote(el.text)

    -- Set language to empty string if it's nil
    -- This is a hack to force backtick code blocks instead of indented code blocks
    -- See https://github.com/jgm/pandoc/issues/7033
    if el.classes[1] == nil then
        el.classes[1] = ''
    end

    return el
end

function Quoted(el)
    -- Normalize quotes
    local quote = el.quotetype == 'SingleQuote' and "'" or '"'
    local inlines = el.content
    table.insert(inlines, 1, quote)
    table.insert(inlines, quote)
    return inlines
end

function Str(el)
    -- Normalize quotes
    el.text = normalize_quote(el.text)

    -- en dash/em dash/non-breaking space would still show up correctly if we don't escape them,
    -- but they would be hardly noticeable while editing.
    local res = {}
    local part = ''
    for c in el.text:gmatch(utf8.charpattern) do
        if c == '\u{2013}' then
            -- en dash
            if part ~= '' then
                table.insert(res, pandoc.Str(part))
                part = ''
            end
            table.insert(res, pandoc.RawInline('html', '&ndash;'))
        elseif c == '\u{2014}' then
            -- em dash
            if part ~= '' then
                table.insert(res, pandoc.Str(part))
                part = ''
            end
            table.insert(res, pandoc.RawInline('html', '&mdash;'))
        elseif c == '\u{00A0}' then
            -- Non-breaking space
            if part ~= '' then
                table.insert(res, pandoc.Str(part))
                part = ''
            end
            table.insert(res, pandoc.RawInline('html', '&nbsp;'))
        else
            part = part .. c
        end
    end
    if part ~= '' then
        table.insert(res, pandoc.Str(part))
    end

    return res
end

function Div(el)
    if el.classes[1] == 'center' then
        local res = {}
        table.insert(res, pandoc.RawBlock('markdown', '<' .. el.classes[1] .. '>'))
        for _, block in ipairs(el.content) do
            table.insert(res, block)
        end
        table.insert(res, pandoc.RawBlock('markdown', '</' .. el.classes[1] .. '>'))
        return res

    elseif el.classes[1] == 'epigraph' then
        local filter = {
            Math = Math,
            Code = Code,
            Quoted = Quoted,
            Str = Str,
            Para = function (s)
                return pandoc.Plain(s.content)
            end,
            Span = function (s)
                return s.content
            end
        }

        function renderHTML(el)
            local doc = pandoc.Pandoc({el})
            local rendered = pandoc.write(doc:walk(filter), 'html')
            return pandoc.RawBlock('markdown', rendered)
        end

        local res = {}
        table.insert(res, pandoc.RawBlock('markdown', '<div style="margin-left: 67%;">'))
        if el.content[1] then
            table.insert(res, renderHTML(el.content[1]))
        end
        table.insert(res, pandoc.RawBlock('markdown', '<div style="border-top: 1px solid #888;"></div>'))
        if el.content[2] then
            table.insert(res, renderHTML(el.content[2]))
        end
        table.insert(res, pandoc.RawBlock('markdown', '</div>'))
        return res
    end

    return nil
end
"""


# Polygon uses some custom macros: https://polygon.codeforces.com/docs/statements-tex-manual
# For example, \bf is deprecated in modern LaTeX, but Polygon treats it the same as \textbf
# and recommends writing \bf{...} instead of \textbf{...} for brevity.
# Similar for \it, \tt, \t
# We just redefine them to their modern counterparts.
# Note that this would break {\bf abcd}, but AFAIK Polygon never recommends that so it's fine.
TEX_MACROS = r"""
\renewcommand{\bf}{\textbf}
\renewcommand{\it}{\textit}
\renewcommand{\tt}{\texttt}
\renewcommand{\t}{\texttt}
"""


def pandoc_tex_to_markdown(tex):
    tex = TEX_MACROS + tex
    with tempfile.TemporaryDirectory() as tmp_dir:
        with open(os.path.join(tmp_dir, 'temp.tex'), 'w', encoding='utf-8') as f:
            f.write(tex)

        with open(os.path.join(tmp_dir, 'filter.lua'), 'w', encoding='utf-8') as f:
            f.write(PANDOC_FILTER)

        subprocess.run(
            ['pandoc', '--lua-filter=filter.lua', '-t', 'gfm', '-o', 'temp.md', 'temp.tex'],
            cwd=tmp_dir,
            check=True,
        )

        with open(os.path.join(tmp_dir, 'temp.md'), 'r', encoding='utf-8') as f:
            md = f.read()

    return md


def pandoc_get_version():
    parts = subprocess.check_output(['pandoc', '--version']).decode().splitlines()[0].split(' ')[1].split('.')
    return tuple(map(int, parts))


def validate_pandoc():
    if not shutil.which('pandoc'):
        raise CommandError('pandoc not installed')
    if pandoc_get_version() < (3, 0, 0):
        raise CommandError('pandoc version must be at least 3.0.0')


def _pick_from_choices(prompt, choices, *, interactive=True, input_func=input, output_func=print):
    if not choices:
        raise CommandError('no choices available')

    if not interactive:
        return choices[0]

    while True:
        choice = input_func(prompt)
        if choice in choices:
            return choice
        output_func('Invalid choice')


def resolve_profiles(usernames):
    profiles = []
    for username in usernames:
        try:
            profile = Profile.objects.get(user__username=username)
        except Profile.DoesNotExist:
            raise CommandError(f'user {username} does not exist')
        profiles.append(profile)
    return profiles


def parse_assets(problem_meta, root, package):
    # Parse interactor
    interactor = root.find('.//interactor')
    if interactor is None:
        print('Use standard grader')
        problem_meta['grader'] = 'standard'
    else:
        print('Found interactor')
        print('Use interactive grader')
        problem_meta['grader'] = 'interactive'
        problem_meta['custom_grader'] = os.path.join(problem_meta['tmp_dir'].name, 'interactor.cpp')

        source = interactor.find('source')
        if source is None:
            raise CommandError('interactor source not found. how possible?')

        path = source.get('path')
        if not path.lower().endswith('.cpp'):
            raise CommandError('interactor must use C++')

        with open(problem_meta['custom_grader'], 'wb') as f:
            f.write(package.read(path))

        print('NOTE: checker is ignored when using interactive grader')
        print('If you use custom checker, please merge it with the interactor')
        problem_meta['checker'] = 'standard'
        return

    # Parse checker
    checker = root.find('.//checker')
    if checker is None:
        raise CommandError('checker not found')

    if checker.get('type') != 'testlib':
        raise CommandError('not a testlib checker. how possible?')

    checker_name = checker.get('name')
    if checker_name is None:
        problem_meta['checker'] = 'bridged'
    else:
        if checker_name in ['std::hcmp.cpp', 'std::ncmp.cpp', 'std::wcmp.cpp']:
            problem_meta['checker'] = 'standard'
            print('Use standard checker')
        elif checker_name in ['std::rcmp4.cpp', 'std::rcmp6.cpp', 'std::rcmp9.cpp']:
            problem_meta['checker'] = 'floats'
            problem_meta['checker_args'] = {'precision': int(checker_name[9])}
            print(f'Use floats checker with precision {problem_meta["checker_args"]["precision"]}')
        elif checker_name == 'std::fcmp.cpp':
            problem_meta['checker'] = 'identical'
            print('Use identical checker')
        elif checker_name == 'std::lcmp.cpp':
            problem_meta['checker'] = 'linecount'
            print('Use linecount checker')
        else:
            problem_meta['checker'] = 'bridged'

    if problem_meta['checker'] == 'bridged':
        print('Use custom checker')

        source = checker.find('source')
        if source is None:
            raise CommandError('checker source not found. how possible?')

        # TODO: support more checkers?
        path = source.get('path')
        if not path.lower().endswith('.cpp'):
            raise CommandError('checker must use C++')

        problem_meta['checker_args'] = {
            'files': 'checker.cpp',
            'lang': 'CPP17',
            'type': 'testlib',
        }

        problem_meta['custom_checker'] = os.path.join(problem_meta['tmp_dir'].name, 'checker.cpp')
        with open(problem_meta['custom_checker'], 'wb') as f:
            f.write(package.read(path))


def parse_tests(problem_meta, root, package):
    testset = root.find('.//testset[@name="tests"]')
    if testset is None:
        raise CommandError('testset tests not found')

    if len(testset.find('tests').getchildren()) == 0:
        raise CommandError('no testcases found')

    # Polygon specifies the time limit in ms and memory limit in bytes,
    # while DMOJ uses seconds and kilobytes.
    problem_meta['time_limit'] = float(testset.find('time-limit').text) / 1000
    problem_meta['memory_limit'] = int(testset.find('memory-limit').text) // 1024

    if hasattr(settings, 'DMOJ_PROBLEM_MIN_MEMORY_LIMIT'):
        problem_meta['memory_limit'] = max(problem_meta['memory_limit'], settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT)
    if hasattr(settings, 'DMOJ_PROBLEM_MAX_MEMORY_LIMIT'):
        problem_meta['memory_limit'] = min(problem_meta['memory_limit'], settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT)

    print(f'Time limit: {problem_meta["time_limit"]}s')
    print(f'Memory limit: {problem_meta["memory_limit"] // 1024}MB')

    problem_meta['cases_data'] = []
    problem_meta['batches'] = {}
    problem_meta['normal_cases'] = []
    problem_meta['zipfile'] = os.path.join(problem_meta['tmp_dir'].name, 'tests.zip')

    # Tests can be aggregated into batches (called groups in Polygon).
    # Each batch can have one of two point policies:
    #    - complete-group: contestant gets points only if all tests in the batch are solved.
    #    - each-test: contestant gets points for each test solved
    # Our judge only supports complete-group batches.
    # For each-test batches, their tests are added as normal tests.
    # Each batch can also have a list of dependencies, which are other batches
    # that must be fully solved before the batch is run.
    # To support dependencies, we just add all dependent tests before the actual tests.
    # (There is actually a more elegant way to do this by using field `dependencies` in init.yml,
    # but site does not support it yet)
    # Our judge does cache result for each test, so the same test will not be run twice.
    # In addition, we only support dependencies for complete-group batches.
    # (Technically, we could support dependencies for each-test batch by splitting it
    # into multiple complete-group batches, but that's too complicated)

    groups = testset.find('groups')
    if groups is not None:
        for group in groups.getchildren():
            name = group.get('name')
            points = float(group.get('points', 0))
            points_policy = group.get('points-policy')
            dependencies = group.find('dependencies')
            if dependencies is None:
                dependencies = []
            else:
                dependencies = [d.get('group') for d in dependencies.getchildren()]

            assert points_policy in ['complete-group', 'each-test']
            if points_policy == 'each-test' and len(dependencies) > 0:
                raise CommandError('dependencies are only supported for batches with complete-group point policy')

            problem_meta['batches'][name] = {
                'name': name,
                'points': points,
                'points_policy': points_policy,
                'dependencies': dependencies,
                'cases': [],
            }

    with open_zipfile_for_write(problem_meta['zipfile']) as tests_zip:
        input_path_pattern = testset.find('input-path-pattern').text
        answer_path_pattern = testset.find('answer-path-pattern').text
        for i, test in enumerate(testset.find('tests').getchildren()):
            points = float(test.get('points', 0))
            input_path = input_path_pattern % (i + 1)
            answer_path = answer_path_pattern % (i + 1)
            input_file = f'{(i + 1):02d}.inp'
            output_file = f'{(i + 1):02d}.out'

            tests_zip.writestr(input_file, package.read(input_path))
            tests_zip.writestr(output_file, package.read(answer_path))

            problem_meta['cases_data'].append({
                'index': i,
                'input_file': input_file,
                'output_file': output_file,
                'points': points,
            })

            group = test.get('group', '')
            if group in problem_meta['batches']:
                problem_meta['batches'][group]['cases'].append(i)
            else:
                problem_meta['normal_cases'].append(i)

    def get_tests_by_batch(name):
        batch = problem_meta['batches'][name]

        if len(batch['dependencies']) == 0:
            return batch['cases']

        # Polygon guarantees no cycles
        cases = set(batch['cases'])
        for dependency in batch['dependencies']:
            cases.update(get_tests_by_batch(dependency))

        batch['dependencies'] = []
        batch['cases'] = list(cases)
        return batch['cases']

    each_test_batches = []
    for batch in problem_meta['batches'].values():
        if batch['points_policy'] == 'each-test':
            each_test_batches.append(batch['name'])
            problem_meta['normal_cases'] += batch['cases']
            continue

        batch['cases'] = get_tests_by_batch(batch['name'])

    for batch in each_test_batches:
        del problem_meta['batches'][batch]

    # Normalize points if necessary
    # Polygon allows fractional points, but DMOJ does not
    all_points = [batch['points'] for batch in problem_meta['batches'].values()] + \
                 [problem_meta['cases_data'][i]['points'] for i in problem_meta['normal_cases']]
    if any(not p.is_integer() for p in all_points):
        print('Found fractional points. Normalize to integers')
        all_points = [int(p * 1000) for p in all_points]
        gcd = math.gcd(*all_points)
        for batch in problem_meta['batches'].values():
            batch['points'] = int(batch['points'] * 1000) // gcd
        for i in problem_meta['normal_cases']:
            case_data = problem_meta['cases_data'][i]
            case_data['points'] = int(case_data['points'] * 1000) // gcd

    # Ignore zero-point batches
    zero_point_batches = [name for name, batch in problem_meta['batches'].items() if batch['points'] == 0]
    if len(zero_point_batches) > 0:
        print('Found zero-point batches:', ', '.join(zero_point_batches))
        ignore_zero_point_batches = problem_meta.get('ignore_zero_point_batches')
        if ignore_zero_point_batches is None and problem_meta.get('interactive', True):
            input_func = problem_meta.get('input_func', input)
            print('Would you like ignore them (y/n)? ', end='', flush=True)
            ignore_zero_point_batches = input_func().lower() in ['y', 'yes']
        if ignore_zero_point_batches:
            problem_meta['batches'] = {
                name: batch for name, batch in problem_meta['batches'].items() if batch['points'] > 0
            }
            print(f'Ignored {len(zero_point_batches)} zero-point batches')

    # Ignore zero-point cases in non-batched tests
    zero_point_cases = [i for i in problem_meta['normal_cases'] if problem_meta['cases_data'][i]['points'] == 0]
    if len(zero_point_cases) > 0:
        print(f'Found {len(zero_point_cases)} zero-point cases')
        ignore_zero_point_cases = problem_meta.get('ignore_zero_point_cases')
        if ignore_zero_point_cases is None and problem_meta.get('interactive', True):
            input_func = problem_meta.get('input_func', input)
            print('Would you like ignore them (y/n)? ', end='', flush=True)
            ignore_zero_point_cases = input_func().lower() in ['y', 'yes']
        if ignore_zero_point_cases:
            problem_meta['normal_cases'] = [
                i for i in problem_meta['normal_cases'] if problem_meta['cases_data'][i]['points'] > 0
            ]
            print(f'Ignored {len(zero_point_cases)} zero-point cases')

    # Sort tests by index
    problem_meta['normal_cases'].sort()
    for batch in problem_meta['batches'].values():
        batch['cases'].sort()

    print(f'Found {len(testset.find("tests").getchildren())} tests!')
    print(f'Parsed as {len(problem_meta["batches"])} batches and {len(problem_meta["normal_cases"])} normal tests!')

    total_points = (sum(b['points'] for b in problem_meta['batches'].values()) +
                    sum(problem_meta['cases_data'][i]['points'] for i in problem_meta['normal_cases']))
    if total_points == 0:
        print('Total points is zero. Set partial to False')
        problem_meta['partial'] = False
    else:
        print('Total points is non-zero. Set partial to True')
        problem_meta['partial'] = True

    problem_meta['grader_args'] = {}
    judging = root.find('.//judging')
    if judging is not None:
        io_input_file = judging.get('input-file', '')
        io_output_file = judging.get('output-file', '')

        if io_input_file != '' and io_output_file != '':
            print('Use File IO')
            print('Input file:', io_input_file)
            print('Output file:', io_output_file)
            problem_meta['grader_args']['io_method'] = 'file'
            problem_meta['grader_args']['io_input_file'] = io_input_file
            problem_meta['grader_args']['io_output_file'] = io_output_file


def parse_statements(problem_meta, root, package):
    # Set default values
    problem_meta['name'] = ''
    problem_meta['description'] = ''
    problem_meta['translations'] = []
    problem_meta['tutorial'] = ''
    interactive = problem_meta.get('interactive', True)
    input_func = problem_meta.get('input_func', input)
    output_func = problem_meta.get('output_func', print)

    def process_images(text, statement_folder):
        image_cache = problem_meta['image_cache']

        def save_image(image_path):
            norm_path = os.path.normpath(os.path.join(statement_folder, image_path))
            sha1 = hashlib.sha1()
            sha1.update(package.open(norm_path, 'r').read())
            sha1 = sha1.hexdigest()

            if sha1 not in image_cache:
                image = File(
                    file=package.open(norm_path, 'r'),
                    name=os.path.basename(image_path),
                )
                data = json.loads(django_uploader(image))
                image_cache[sha1] = data['link']

            return image_cache[sha1]

        for image_path in set(re.findall(r'!\[image\]\((.+?)\)', text)):
            text = text.replace(
                f'![image]({image_path})',
                f'![image]({save_image(image_path)})',
            )

        for img_tag in set(re.findall(r'<\s*img[^>]*>', text)):
            image_path = re.search(r'<\s*img[^>]+src\s*=\s*(["\'])(.*?)\1[^>]*>', img_tag).group(2)
            text = text.replace(
                img_tag,
                img_tag.replace(image_path, save_image(image_path)),
            )

        return text

    def parse_problem_properties(problem_properties):
        description = ''

        # Legend
        description += pandoc_tex_to_markdown(problem_properties['legend'])

        # Input
        description += '\n## Input\n\n'
        description += pandoc_tex_to_markdown(problem_properties['input'])

        # Output
        description += '\n## Output\n\n'
        description += pandoc_tex_to_markdown(problem_properties['output'])

        # Interaction
        if problem_properties['interaction'] is not None:
            description += '\n## Interaction\n\n'
            description += pandoc_tex_to_markdown(problem_properties['interaction'])

        # Scoring
        if problem_properties['scoring'] is not None:
            description += '\n## Scoring\n\n'
            description += pandoc_tex_to_markdown(problem_properties['scoring'])

        # Sample tests
        for i, sample in enumerate(problem_properties['sampleTests'], start=1):
            description += f'\n## Sample Input {i}\n\n'
            description += '```\n' + sample['input'].strip() + '\n```\n'
            description += f'\n## Sample Output {i}\n\n'
            description += '```\n' + sample['output'].strip() + '\n```\n'

        # Notes
        if problem_properties['notes'] != '':
            description += '\n## Notes\n\n'
            description += pandoc_tex_to_markdown(problem_properties['notes'])

        return description

    statements = root.findall('.//statement[@type="application/x-tex"]')
    if len(statements) == 0:
        allow_missing_statement = problem_meta.get('allow_missing_statement')
        if allow_missing_statement is None and interactive:
            print('Statement not found! Would you like to skip statement (y/n)? ', end='', flush=True)
            allow_missing_statement = input_func().lower() in ['y', 'yes']
        if allow_missing_statement:
            return

        raise CommandError('statement not found')

    translations = []
    tutorials = []
    for statement in statements:
        language = statement.get('language', 'unknown')
        statement_folder = os.path.dirname(statement.get('path'))
        problem_properties_path = os.path.join(statement_folder, 'problem-properties.json')
        if problem_properties_path not in package.namelist():
            raise CommandError(f'problem-properties.json not found at path {problem_properties_path}')

        problem_properties = json.loads(package.read(problem_properties_path).decode('utf-8'))

        output_func(f'Converting statement in language {language} to Markdown')
        description = parse_problem_properties(problem_properties)
        translations.append({
            'language': language,
            'description': process_images(description, statement_folder),
            'statement_folder': statement_folder,
        })

        tutorial = problem_properties['tutorial']
        if isinstance(tutorial, str) and tutorial != '':
            output_func(f'Converting tutorial in language {language} to Markdown')
            tutorial = pandoc_tex_to_markdown(tutorial)
            tutorials.append({
                'language': language,
                'tutorial': tutorial,
                'statement_folder': statement_folder,
            })

    main_statement_language = problem_meta.get('main_statement_language')
    if len(translations) > 1:
        languages = [t['language'] for t in translations]
        output_func('Multilingual statements found:', languages)
        if main_statement_language not in languages:
            if not interactive:
                raise CommandError(f'invalid main statement language {main_statement_language}')
            main_statement_language = _pick_from_choices(
                'Please select one as the main statement: ',
                languages,
                interactive=interactive,
                input_func=input_func,
                output_func=output_func,
            )
    else:
        main_statement_language = translations[0]['language']

    main_tutorial_language = problem_meta.get('main_tutorial_language')
    if len(tutorials) > 1:
        languages = [t['language'] for t in tutorials]
        output_func('Multilingual tutorials found:', languages)
        if main_tutorial_language not in languages:
            if not interactive:
                raise CommandError(f'invalid main tutorial language {main_tutorial_language}')
            main_tutorial_language = _pick_from_choices(
                'Please select one as the sole tutorial: ',
                languages,
                interactive=interactive,
                input_func=input_func,
                output_func=output_func,
            )
        selected_tutorial = next(t for t in tutorials if t['language'] == main_tutorial_language)
        problem_meta['tutorial'] = process_images(selected_tutorial['tutorial'], selected_tutorial['statement_folder'])
    elif len(tutorials) > 0:
        selected_tutorial = tutorials[0]
        problem_meta['tutorial'] = process_images(selected_tutorial['tutorial'], selected_tutorial['statement_folder'])

    translation_language_map = problem_meta.get('translation_language_map', {})
    available_site_languages = list(map(itemgetter(0), settings.LANGUAGES))
    for t in translations:
        language = t['language']
        description = t['description']
        name_element = root.find(f'.//name[@language="{language}"]')
        name = name_element.get('value') if name_element is not None else ''

        if language == main_statement_language:
            problem_meta['name'] = name
            problem_meta['description'] = description
        else:
            site_language = translation_language_map.get(language)
            if site_language is not None and site_language not in available_site_languages:
                raise CommandError(f'invalid site language for {language}')

            if site_language is None:
                if language in available_site_languages:
                    site_language = language
                elif not interactive:
                    raise CommandError(f'missing site language mapping for {language}')
                else:
                    site_language = _pick_from_choices(
                        f'Please select corresponding site language for {language} '
                        f'(available options are {", ".join(available_site_languages)}): ',
                        available_site_languages,
                        interactive=interactive,
                        input_func=input_func,
                        output_func=output_func,
                    )
            problem_meta['translations'].append({
                'language': site_language,
                'name': name,
                'description': description,
            })


def parse_solutions(problem_meta, root, package):
    interactive = problem_meta.get('interactive', True)
    input_func = problem_meta.get('input_func', input)
    output_func = problem_meta.get('output_func', print)
    append_main_solution = problem_meta.get('append_main_solution_to_tutorial')

    solutions = root.find('.//solutions')
    if solutions is None:
        return

    main_solution = solutions.find('solution[@tag="main"]')
    if main_solution is None:
        return

    if append_main_solution is not None:
        if not append_main_solution:
            return
    elif interactive:
        output_func('Main solution found. Would you like to append it to the tutorial (y/n)? ', end='', flush=True)
        if input_func().lower() not in ['y', 'yes']:
            return
    else:
        return

    source = main_solution.find('source')
    if source is None:
        return

    source_path = source.get('path')
    if not source_path or source_path not in package.namelist():
        return

    source_code = package.read(source_path).decode('utf-8').strip()
    source_lang = source.get('type', '')
    markdown_lang = ''
    if source_lang.startswith('cpp'):
        markdown_lang = 'cpp'
    elif source_lang.startswith('python'):
        markdown_lang = 'python'
    elif source_lang.startswith('java'):
        markdown_lang = 'java'

    problem_meta['tutorial'] = problem_meta['tutorial'].rstrip() + f"""\n
<blockquote class="spoiler">
```{markdown_lang}
{source_code}
```
</blockquote>
"""


def _sync_problem_testcases(problem, problem_meta):
    ProblemTestCase.objects.filter(dataset=problem).delete()

    order = 0
    for batch in problem_meta['batches'].values():
        if len(batch['cases']) == 0:
            continue

        order += 1
        start_batch = ProblemTestCase(dataset=problem, order=order, type='S', points=batch['points'], is_pretest=False)
        start_batch.save()

        for case_index in batch['cases']:
            order += 1
            case_data = problem_meta['cases_data'][case_index]
            case = ProblemTestCase(
                dataset=problem,
                order=order,
                type='C',
                input_file=case_data['input_file'],
                output_file=case_data['output_file'],
                is_pretest=False,
            )
            case.save()

        order += 1
        end_batch = ProblemTestCase(dataset=problem, order=order, type='E', is_pretest=False)
        end_batch.save()

    for case_index in problem_meta['normal_cases']:
        order += 1
        case_data = problem_meta['cases_data'][case_index]
        case = ProblemTestCase(
            dataset=problem,
            order=order,
            type='C',
            input_file=case_data['input_file'],
            output_file=case_data['output_file'],
            points=case_data['points'],
            is_pretest=False,
        )
        case.save()


@transaction.atomic
def update_or_create_problem(problem_meta):
    print('Creating/Updating problem in database')
    organization = problem_meta.get('organization')
    do_update = problem_meta.get('do_update', False)
    override_statements = problem_meta.get('override_statements', True)
    existing_problem = Problem.objects.filter(code=problem_meta['code']).first()
    preserve_existing_attrs = bool(do_update and existing_problem is not None)
    uncategorized_group = ProblemGroup.objects.order_by('id').first()
    is_organization_private = (
        bool(organization) or bool(existing_problem and existing_problem.is_organization_private)
    )
    statement_name = (
        existing_problem.name
        if preserve_existing_attrs and not override_statements
        else problem_meta['name']
    )
    statement_description = (
        existing_problem.description
        if preserve_existing_attrs and not override_statements
        else problem_meta['description']
    )
    group_value = existing_problem.group if preserve_existing_attrs else uncategorized_group
    points_value = existing_problem.points if preserve_existing_attrs else 0.0

    problem, _ = Problem.objects.update_or_create(
        code=problem_meta['code'],
        defaults={
            'name': statement_name,
            'time_limit': problem_meta['time_limit'],
            'memory_limit': problem_meta['memory_limit'],
            'description': statement_description,
            'partial': problem_meta['partial'],
            'group': group_value,
            'points': points_value,
            'is_organization_private': is_organization_private,
        },
    )
    problem.save()
    if organization is not None:
        problem.organizations.add(organization)
    problem.allowed_languages.set(Language.objects.filter(include_in_problem=True))
    problem.authors.set(problem_meta['authors'])
    problem.curators.set(problem_meta['curators'])
    if not preserve_existing_attrs:
        problem.types.set([ProblemType.objects.order_by('id').first()])  # Uncategorized
    problem.save()

    if not preserve_existing_attrs or override_statements:
        ProblemTranslation.objects.filter(problem=problem).delete()
        for tran in problem_meta['translations']:
            ProblemTranslation(
                problem=problem,
                language=tran['language'],
                name=tran['name'],
                description=tran['description'],
            ).save()

        Solution.objects.filter(problem=problem).delete()
        if problem_meta['tutorial'].strip() != '':
            Solution(
                problem=problem,
                is_public=False,
                publish_on=timezone.now(),
                content=problem_meta['tutorial'].strip(),
            ).save()

    problem_data, _ = ProblemData.objects.get_or_create(problem=problem)
    with open(problem_meta['zipfile'], 'rb') as f:
        zipfile_name = f'{problem_meta["code"]}_{timezone.now().strftime("%Y%m%d%H%M%S%f")}.zip'
        problem_data.zipfile.save(zipfile_name, File(f), save=False)
        problem_data.grader = problem_meta['grader']
        problem_data.checker = problem_meta['checker']
        problem_data.grader_args = json.dumps(problem_meta['grader_args'])
        problem_data.save()

    if problem_meta['checker'] == 'bridged':
        with open(problem_meta['custom_checker'], 'rb') as f:
            problem_data.custom_checker = File(f)
            problem_data.save()

    if 'checker_args' in problem_meta:
        problem_data.checker_args = json.dumps(problem_meta['checker_args'])
        problem_data.save()

    if 'custom_grader' in problem_meta:
        with open(problem_meta['custom_grader'], 'rb') as f:
            problem_data.custom_grader = File(f)
            problem_data.save()

    _sync_problem_testcases(problem, problem_meta)

    print('Generating init.yml')
    ProblemDataCompiler.generate(
        problem=problem,
        data=problem_data,
        cases=problem.cases.order_by('order'),
        files=zipfile.ZipFile(problem_data.zipfile.path).namelist(),
    )
    return problem


def import_polygon_package(
        package,
        *,
        problem_code,
        authors=(),
        curators=(),
        organization=None,
        interactive=True,
        input_func=input,
        output_func=print,
        ignore_zero_point_batches=None,
        ignore_zero_point_cases=None,
        allow_missing_statement=None,
        main_statement_language=None,
        main_tutorial_language=None,
        translation_language_map=None,
        do_update=False,
        append_main_solution_to_tutorial=None,
        override_statements=True):
    validate_pandoc()

    # Let's validate the problem code right now.
    # We don't want to have done everything and still fail because
    # of invalid problem code.
    Problem._meta.get_field('code').run_validators(problem_code)
    if Problem.objects.filter(code=problem_code).exists() and not do_update:
        raise CommandError(f'problem with code {problem_code} already exists')

    if hasattr(package, 'seek'):
        package.seek(0)

    with zipfile.ZipFile(package, 'r') as package_file:
        if 'problem.xml' not in package_file.namelist():
            raise CommandError('problem.xml not found')
        root = ET.fromstring(package_file.read('problem.xml'))

        # A dictionary to hold all problem information.
        problem_meta = {
            'image_cache': {},
            'code': problem_code,
            'tmp_dir': tempfile.TemporaryDirectory(),
            'authors': list(authors),
            'curators': list(curators),
            'organization': organization,
            'interactive': interactive,
            'input_func': input_func,
            'output_func': output_func,
            'ignore_zero_point_batches': ignore_zero_point_batches,
            'ignore_zero_point_cases': ignore_zero_point_cases,
            'allow_missing_statement': allow_missing_statement,
            'main_statement_language': main_statement_language,
            'main_tutorial_language': main_tutorial_language,
            'translation_language_map': translation_language_map or {},
            'do_update': do_update,
            'append_main_solution_to_tutorial': append_main_solution_to_tutorial,
            'override_statements': override_statements,
            'name': '',
            'description': '',
            'translations': [],
            'tutorial': '',
        }

        try:
            parse_assets(problem_meta, root, package_file)
            parse_tests(problem_meta, root, package_file)
            if not (do_update and not override_statements):
                parse_statements(problem_meta, root, package_file)
                parse_solutions(problem_meta, root, package_file)
            return update_or_create_problem(problem_meta)
        except Exception:
            # Remove imported images
            for image_url in problem_meta['image_cache'].values():
                path = default_storage.path(os.path.join(settings.MARTOR_UPLOAD_MEDIA_DIR, os.path.basename(image_url)))
                if os.path.exists(path):
                    os.remove(path)
            raise
        finally:
            problem_meta['tmp_dir'].cleanup()
