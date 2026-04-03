import re

TRIVIAL_COMMENT_PATTERNS = [
    r'increment', r'decrement', r'initialize', r'loop through', r'calculate', r'return',
    r'check if', r'helper function', r'iterate', r'create', r'update', r'set value',
    # Add more specific C++ related trivial comments if needed
    r'end of loop', r'start of loop', r'variable declaration', r'function definition', r'include all', r'function to', r'read input', r'output the'
]

def get_cpp_comment_details(code_str: str) -> tuple[list[str], list[str]]:
    """
    Identifies lines that are primarily C++ comments and extracts the content of these comments.
    A line is "primarily a comment" if it starts with // or is entirely within a /* ... */ block,
    without preceding code on the same line.

    Args:
        code_str: The C++ source code.

    Returns:
        A tuple containing:
        - list_of_comment_lines: Full original lines identified as comment lines.
        - list_of_actual_comment_contents: The textual content of the comments.
    """
    identified_comment_lines = []
    actual_comment_contents = []
    lines = code_str.splitlines()
    in_block_comment = False

    for line_content in lines:
        stripped_line = line_content.strip()

        # Skip blank lines for this specific analysis of comment *lines*
        # (though they are skipped later for overall non-empty lines)
        if not stripped_line:
            continue

        is_line_predominantly_comment = False
        current_processing_line = stripped_line # Use stripped line for logic

        # Store comment text found on this line
        comment_text_this_line_parts = []

        if in_block_comment:
            is_line_predominantly_comment = True # Tentatively
            end_block_idx = current_processing_line.find("*/")
            if end_block_idx != -1:
                # Block comment ends on this line
                comment_part = current_processing_line[:end_block_idx]
                # Clean comment part (e.g. leading '*')
                if comment_part.startswith('*'):
                    comment_part = comment_part[1:].strip()
                else:
                    comment_part = comment_part.strip()
                if comment_part:
                    comment_text_this_line_parts.append(comment_part)
                
                in_block_comment = False
                # Check if there's non-comment code AFTER '*/'
                if current_processing_line[end_block_idx+2:].strip():
                    is_line_predominantly_comment = False # Has code after comment ends
            else:
                # Entire line is within the multi-line comment
                comment_part = current_processing_line
                if comment_part.startswith('*'):
                    comment_part = comment_part[1:].strip()
                else:
                    comment_part = comment_part.strip()
                if comment_part:
                    comment_text_this_line_parts.append(comment_part)
        
        else: # Not currently in a multi-line comment from a previous line
            # Important: Handle in-line comments like `code(); // comment` or `code(); /* comment */`
            # These lines are NOT "comment lines" by the definition "primarily a comment"
            
            # Check for start of single-line comment first
            single_line_comment_idx = current_processing_line.find("//")
            if single_line_comment_idx != -1:
                code_before_single = current_processing_line[:single_line_comment_idx].strip()
                if not code_before_single: # Line starts with // (or only whitespace before)
                    is_line_predominantly_comment = True
                    comment_text_this_line_parts.append(current_processing_line[single_line_comment_idx+2:].strip())
                # else: it's an inline comment after code, line is not "predominantly comment"
            
            # Check for start of block comment if not already handled by single-line
            if not is_line_predominantly_comment:
                start_block_idx = current_processing_line.find("/*")
                if start_block_idx != -1:
                    code_before_block = current_processing_line[:start_block_idx].strip()
                    if not code_before_block: # Line starts with /* (or only whitespace before)
                        is_line_predominantly_comment = True
                        comment_part_in_block = current_processing_line[start_block_idx+2:]
                        end_block_idx_on_line = comment_part_in_block.find("*/")
                        
                        if end_block_idx_on_line != -1:
                            # Block comment also ends on this line "/* ... */"
                            actual_comment_text = comment_part_in_block[:end_block_idx_on_line].strip()
                            if actual_comment_text:
                                comment_text_this_line_parts.append(actual_comment_text)
                            # Check if there's non-comment code AFTER "*/"
                            if comment_part_in_block[end_block_idx_on_line+2:].strip():
                                is_line_predominantly_comment = False # Code after */
                        else:
                            # Block comment starts but does not end on this line
                            if comment_part_in_block.strip(): # Add content if any before EOL
                                comment_text_this_line_parts.append(comment_part_in_block.strip())
                            in_block_comment = True 
                    # else: it's an inline block comment after code, line is not "predominantly comment"

        if is_line_predominantly_comment:
            identified_comment_lines.append(line_content) # Add the original line
            for part in comment_text_this_line_parts: # Add extracted comment texts
                if part: # Ensure non-empty parts
                    actual_comment_contents.append(part)
    
    return identified_comment_lines, actual_comment_contents

def extract_variable_names(code: str):
    # User's original pattern for finding variable names after a type keyword.
    # This is a heuristic and will not capture all C++ variable declaration styles.
    # e.g., int a, b; (will only find 'a'), int* p; (might find 'p' but misses pointer context for length analysis)
    pattern = r'\b(?:int|long|double|float|string|char|bool|auto)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
    variable_names = re.findall(pattern, code)
    return list(set(variable_names)) # Remove duplicates

def count_functions(code: str):
    # User's original pattern for counting function definitions.
    # This is also a heuristic.
    pattern = r'\b(?:int|void|bool|long|double|string|auto)\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\([^)]*\)\s*{'
    return len(re.findall(pattern, code))

def contains_trivial_comment(comment_text: str): # Expects actual comment content
    return any(re.search(p, comment_text.lower()) for p in TRIVIAL_COMMENT_PATTERNS)

def analyze_cpp_code(code: str) -> dict:
    # Get all non-empty lines from the code
    all_lines = code.splitlines()
    code_lines_non_empty = [line.strip() for line in all_lines if line.strip()]

    if not code_lines_non_empty:
        return {"ai_generated": False, "reason": ["Empty code"]}

    # Use the improved comment identification to get lines that are comments
    # and the actual textual content of those comments.
    identified_comment_lines, actual_comment_contents = get_cpp_comment_details(code)
    
    num_comment_lines = len(identified_comment_lines)
    num_total_non_empty_lines = len(code_lines_non_empty)

    # Calculate comment ratio based on non-empty lines
    comment_ratio = num_comment_lines / num_total_non_empty_lines if num_total_non_empty_lines > 0 else 0.0
    
    # Check for trivial comments using the extracted comment contents
    trivial_comments_count = sum(1 for c_content in actual_comment_contents if contains_trivial_comment(c_content))

    variable_names = extract_variable_names(code)
    long_vars = [v for v in variable_names if len(v) > 25] # Variables longer than 25 chars
    
    # Calculate average variable length, handle division by zero if no variables found
    avg_var_length = sum(len(v) for v in variable_names) / len(variable_names) if variable_names else 0.0

    function_count = count_functions(code)

    reasons = []
    thresholds = {
        "comment_ratio": 0.15,       # Original: 0.15; Your problem description used > 30%
        "avg_var_length": 15.0,      # Original: 15
        "function_count": 10,        # Original: 10
        "max_var_len_individual": 25 # Implied
    }

    # Your original problem description said >30% for comments.
    # The provided code uses >0.15. I'll use 0.15 as per your `analyze_cpp_code` snippet.
    if comment_ratio > thresholds["comment_ratio"]:
        reasons.append(f"High comment ratio ({comment_ratio:.2f})")

    if avg_var_length > thresholds["avg_var_length"]:
        reasons.append(f"Average variable name length too high ({avg_var_length:.2f})")

    if long_vars:
        reasons.append(f"Found {len(long_vars)} long variable name(s) (>{thresholds['max_var_len_individual']} chars): {', '.join(long_vars[:3])}{'...' if len(long_vars) > 3 else ''}")

    if trivial_comments_count > 0:
        reasons.append(f"{trivial_comments_count} trivial comment(s) found")

    # Note: A high number of trivial comments might be normal for well-explained beginner code.
    # Consider if this heuristic is too aggressive or needs context.

    if function_count > thresholds["function_count"]:
        reasons.append(f"High function count ({function_count})")

    is_ai_flag = bool(reasons)
    return {
        "ai_generated": is_ai_flag,
        "reason": reasons if is_ai_flag else ["No suspicious traits detected"] # Ensure 'reason' is always a list
    }