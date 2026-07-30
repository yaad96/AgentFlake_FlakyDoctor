import ast
import csv
import json
import os
import sys
import subprocess
import javalang
from typing import Set, Tuple
import sys
import re

checkout_project_cmds = "src/cmds/checkout_project.sh"
stash_project_cmds = "src/cmds/stash_project.sh"

def get_helper_methods(code):
    method_list = parse_java_func_intervals(code)
    start_lines = []
    res = {"before":{},"after":{},"earlist_line":{},"method_names":[]}
    for method_info in method_list:
        start, end, method_name, method_code, node = method_info[0:]
        start_lines.append(start.line)
        if node.annotations != None:
            for ele in node.annotations:
                if ele.name == "BeforeClass" or ele.name == "Before" or ele.name == "BeforeAll":
                    if method_name not in res["before"]:
                        method_code = get_string(code,start,end)
                        res["before"][method_name] = method_code
                        res["method_names"].append(method_name)
                elif ele.name == "AfterClass" or ele.name == "After" or ele.name == "AfterAll":
                    if method_name not in res["after"]:
                        method_code = get_string(code,start,end)
                        res["after"][method_name] = method_code
                        res["method_names"].append(method_name)
    res["earlist_line"] = min(start_lines)
    return res

def get_global_vars(code,start_line):
    fields = {}
    trees = javalang.parse.parse(code)
    for _, node in javalang.parse.parse(code):
        func_intervals = set()
        if isinstance(
            node,
            (javalang.tree.FieldDeclaration),
        ):
            stat = get_string(code,node.start_position,node.end_position),
            node_name = node.declarators[0].name,
            # func_intervals.add(
            #     (
            #         node.start_position,
            #         node.end_position,
            #         stat,
            #         node
            #     )
            # )
            if node.start_position.line >= start_line:
                continue
            if node_name not in fields:
                fields[node_name[0]] = stat[0]
    return fields

def git_checkout_file(projectDir,file_path):
    result = subprocess.run(["bash",checkout_project_cmds,projectDir,file_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = result.stdout.decode('utf-8')
    print(output)

def git_stash(projectDir):
    result = subprocess.run(["bash",stash_project_cmds,projectDir], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = result.stdout.decode('utf-8')
    print(output)

def get_package(code):
    try:
        trees = javalang.parse.parse(code)
        if trees.package:
            full_package = "package " + trees.package.name + ";"
            return full_package
    except:
        print("package not found")
        return None

def replace_last_symbol(source_string, replace_what, replace_with):
    head, _sep, tail = source_string.rpartition(replace_what)
    return head + replace_with + tail

def get_imports(code):
    original_code = code
    imp_list = []
    try:
        trees = javalang.parse.parse(code)
        if trees.imports:
            for import_node in trees.imports:
                import_stat = get_string(original_code,import_node.start_position,import_node.end_position)
                imp_list.append(import_stat)
    except:
        return imp_list
    return imp_list

def get_string(data, start, end):
    if start is None:
        return ""

    end_pos = None

    if end is not None:
        end_pos = end.line #- 1

    lines = data.splitlines(True)
    string = "".join(lines[start.line:end_pos])
    string = lines[start.line - 1] + string

    if end is None:
        left = string.count("{")
        right = string.count("}")
        if right - left == 1:
            p = string.rfind("}")
            string = string[:p]

    return string

def parse_java_func_intervals(content: str) -> Set[Tuple[int, int]]:
    func_intervals = set()
    try:
    # if True:
        # trees = javalang.parse.parse(content)
        # print(trees)
        for _, node in javalang.parse.parse(content):
            if isinstance(
                node,
                (javalang.tree.MethodDeclaration, javalang.tree.ConstructorDeclaration),
            ):
                func_intervals.add(
                    (
                        node.start_position,
                        node.end_position,
                        node.name,
                        get_string(content,node.start_position,node.end_position),
                        node
                    )
                )
        return func_intervals
    except Exception as e: # javalang.parser.JavaSyntaxError
        print("Expceptions", e)
        return func_intervals

def extract_method(test_name,class_content):
    method_list = parse_java_func_intervals(class_content)
    res = None
    for method_info in method_list:
        start, end, method_name, method_code, node = method_info[0:]
        if test_name == method_name:
            # print(node.modifiers, node.name, node.parameters, node.return_type, node.throws)
            # if node.annotations != None:
            #     for ele in node.annotations:
            #         if ele.name != "Test":
            #             continue
            # res = [start,end,method_name,method_code,node.annotations]
            # print(node)
            res = [method_code,node, node.modifiers, node.name, node.parameters, node.return_type, node.throws]
    return res

def _mask_java(s):
    """Return a same-length copy of Java source with the contents of string/char
    literals and comments blanked out (newlines preserved). Lets a brace/paren scan
    and a method-name search run without being fooled by braces, parens, or the
    method name appearing inside a string or comment. Indices map 1:1 to the input."""
    out = []
    i, n = 0, len(s)
    NORMAL, LINE, BLOCK, STR, CHAR = 0, 1, 2, 3, 4
    state = NORMAL
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if state == NORMAL:
            if c == "/" and nxt == "/":
                state = LINE; out.append("  "); i += 2; continue
            if c == "/" and nxt == "*":
                state = BLOCK; out.append("  "); i += 2; continue
            if c == '"':
                state = STR; out.append(" "); i += 1; continue
            if c == "'":
                state = CHAR; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == LINE:
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = NORMAL
            i += 1; continue
        if state == BLOCK:
            if c == "*" and nxt == "/":
                state = NORMAL; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        # STR or CHAR
        if c == "\\":
            out.append("  "); i += 2; continue
        if (state == STR and c == '"') or (state == CHAR and c == "'"):
            state = NORMAL; out.append(" "); i += 1; continue
        out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


def _extract_method_text(test_name, class_content):
    """Parser-independent fallback for get_test_method: locate the method named
    `test_name` by text and return its exact source span (whole signature line
    through the whole closing-brace line, matching get_string's convention) via
    brace matching. Returns None if the file has no such method definition. This
    recovers methods javalang can't reach because it failed to parse the file
    (e.g. Java-8 type-use annotations like Box<@From(..) Integer> or Foo @Size(..)[])."""
    masked = _mask_java(class_content)
    pattern = re.compile(r"(?<![A-Za-z0-9_$.])" + re.escape(test_name) + r"\s*\(")
    result = None
    for m in pattern.finditer(masked):
        # match the parameter parens
        p = masked.find("(", m.start())
        depth, i = 0, p
        while i < len(masked):
            ch = masked[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if i >= len(masked):
            continue
        # the next significant char must open a body ('{'); ';' => abstract decl or a call
        j = i + 1
        while j < len(masked) and masked[j] not in "{;":
            j += 1
        if j >= len(masked) or masked[j] == ";":
            continue
        # brace-match the body
        depth, k = 0, j
        while k < len(masked):
            ch = masked[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= len(masked):
            continue
        # expand to full lines: start of the signature line .. end of the closing-brace line
        line_start = class_content.rfind("\n", 0, m.start()) + 1
        nl = class_content.find("\n", k)
        line_end = len(class_content) if nl == -1 else nl + 1
        result = class_content[line_start:line_end]  # keep last match (mirrors get_test_method)
    return result


def get_test_method(test_name,class_content):
    method_list = parse_java_func_intervals(class_content)
    res = None
    for method_info in method_list:
        start, end, method_name, method_code, node = method_info[0:]
        if test_name == method_name:
            # if node.annotations != None:
            #     for ele in node.annotations:
            #         if ele.name != "Test":
            #             continue
            # res = [start,end,method_name,method_code,node.annotations]
            res = method_code
    if res is None:
        # javalang either could not parse the file (JavaSyntaxError -> empty method
        # list) or found no match; recover the method by text so a parser limitation
        # doesn't turn into method_code_location_failure.
        res = _extract_method_text(test_name, class_content)
    return res


def _super_class_name(class_content, class_name=None):
    """Return the immediate superclass simple name for `class_name` (or the first
    class if not given), or None. Uses masked source so `extends` in a comment/string
    or a `<T extends Bound>` type parameter isn't mistaken for the superclass."""
    masked = _mask_java(class_content)
    name = re.escape(class_name) if class_name else r"\w+"
    m = re.search(r"\bclass\s+" + name + r"\b\s*(?:<[^{}]*?>)?\s+extends\s+([\w.]+)", masked)
    return m.group(1).split(".")[-1] if m else None


def _find_class_file(project_dir, simple_name, module=None):
    """Locate <simple_name>.java under project_dir (skipping build output dirs),
    preferring the given module then test/main sources. Returns a path or None."""
    target = simple_name + ".java"
    candidates = []
    for root, _dirs, files in os.walk(project_dir):
        if target in files:
            fp = os.path.join(root, target)
            if "/target/" in fp or "/build/" in fp or "/bin/" in fp:
                continue
            candidates.append(fp)
    if not candidates:
        return None

    def rank(fp):
        s = 0
        if module and module in fp:
            s += 4
        if "/src/test/" in fp:
            s += 2
        elif "/src/main/" in fp:
            s += 1
        return -s  # best (highest) first

    candidates.sort(key=rank)
    return candidates[0]


def resolve_inherited_test_method(method_name, class_simple_name, class_content, project_dir, module=None):
    """When a test method isn't defined in its own class file, follow the `extends`
    chain and search base-class files for it. Returns (file_path, class_content,
    method_code) for the base class that actually defines the method, or None. The
    caller retargets the repair to that file so the fix lands where the code lives."""
    visited = set()
    content = class_content
    cur_name = class_simple_name
    for _ in range(20):  # guard against cycles / pathological hierarchies
        sup = _super_class_name(content, cur_name)
        if not sup or sup in visited:
            return None
        visited.add(sup)
        base_path = _find_class_file(project_dir, sup, module)
        if not base_path:
            return None
        base_content = read_file(base_path)
        code = get_test_method(method_name, base_content)
        if code is not None:
            return (base_path, base_content, code)
        content = base_content
        cur_name = sup
    return None


def write_dict_csv(csv_path, fields, dict_data):
    with open(csv_path, 'a') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fields)
        writer.writerow(dict_data)

def write_header_csv(csv_path, fields):
    dir_name = os.path.dirname(csv_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(csv_path, 'w') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fields)
        writer.writeheader()

def read_file(file_path):
    file = open(file_path, 'r')
    content = file.read()
    return content

def write_file(file_path,content):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
    except:
        pass
    f = open(file_path, "w")
    f.write(content)
    f.close()
    
def add_file(file_path,content):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
    except:
        pass
    f = open(file_path, "a")
    f.write(content)
    f.close()

def write_json(file_path, dict):
    with open(file_path, 'w') as fp:
        json.dump(dict, fp)

def write_json_attach(file_path, dict):
    with open(file_path, 'a') as fp:
        json.dump(dict, fp)

def git_diff(file_path):
    diff_path = file_path.replace(".py","_diff.patch")
    diff_opts = ["git", "diff", file_path] #, "|& tee", diff_path
    print(" ".join(diff_opts), flush=True)
    diff = subprocess.run(diff_opts, stdout=subprocess.PIPE)
    diff_output = diff.stdout.decode('utf-8')
    write_file(diff_path, diff_output)
    # print(diff_output)
    return diff_path

def diff(source_file, target_file):
    diff_path = target_file.replace(".py","_diff.patch")
    diff_opts = ["diff", source_file, target_file]
    diff_output = run_cmds(diff_opts, None)
    write_file(diff_path, diff_output)
    # print(diff_output)
    return diff_output, diff_path

def run_cmds(cmd_list, timeoutVal):
    cmds = " ".join(cmd_list)
    # print(cmds, flush=True)
    if timeoutVal != None:
        run_cmds = subprocess.run(cmd_list, stdout=subprocess.PIPE, timeout=timeoutVal) #check=True, capture_output=True, shell=True
    else:
        run_cmds = subprocess.run(cmd_list, stdout=subprocess.PIPE)
    output = run_cmds.stdout.decode('utf-8')
    # print(output, flush=True)
    return output

def run_cmds_nopipe(cmd_list, timeoutVal):
    cmds = " ".join(cmd_list)
    print(cmds, flush=True)
    if timeoutVal != None:
        run_cmds = subprocess.run(cmds, check=True, capture_output=True, shell=True, timeout=timeoutVal) #check=True, capture_output=True, shell=True
    else:
        run_cmds = subprocess.run(cmds, check=True, capture_output=True, shell=True)
    output = run_cmds.stdout.decode('utf-8')
    # print(output, flush=True)
    return output

def git_checkout(file_path):
    git_checkout_opts = ["git", "checkout", file_path]
    print(" ".join(git_checkout_opts), flush=True)
    git_checkout = subprocess.run(git_checkout_opts, stdout=subprocess.PIPE)
    git_checkout_output = git_checkout.stdout.decode('utf-8')
    return git_checkout_output

def extract_java_code(text):
    lst = text.replace("```java","\n").replace("```","\n").replace("//<fix start>","\n").replace("//<fix end>","\n").split("\n")
    left = 0
    right = 0
    methods = {}
    idx = 0
    method = []
    inAmethod = False
    for line in lst:
        if "public class " in line:
            continue
        idx += 1
        l = line.count("{")
        left += l
        r = line.count("}")
        right += r
        if left == right and right > 0 and inAmethod == True:
            method.append(line)
            left = 0
            right = 0
            methods[str(idx)] = method
            method = []
            inAmethod = False
        elif left > right:
            inAmethod = True
            method.append(line)
        elif line.strip() in ["@Before","@After", "@BeforeEach","@AfterEach","@BeforeAll","@AfterAll","@BeforeClass","@AfterClass"]:
            inAmethod = True
            method.append(line)
    
    dummy_code = "public class Lambda {\n"
    for key in methods:
        method = "\n".join(methods[key]) + "\n"
        dummy_code += method
    dummy_code += "\n}\n"

    # print(dummy_code)

    method_list = parse_java_func_intervals(dummy_code)
    if method_list != None:
        return method_list,True
    else:
        return methods,False