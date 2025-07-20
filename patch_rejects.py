import os
import subprocess
import shutil
import difflib
import re
import argparse
from pathlib import Path

def run_command(command, cwd=None, check=True):
    """Run a shell command and handle errors."""
    result = subprocess.run(command, shell=True, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        print(f"Error running command '{command}': {result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, command)
    return result

def clone_repos(ksu_branch, susfs_branch, work_dir):
    """Clone KernelSU-Next and susfs4ksu repositories with specified branches."""
    os.makedirs(work_dir, exist_ok=True)
    ksu_dir = os.path.join(work_dir, "KernelSU-Next")
    susfs_dir = os.path.join(work_dir, "susfs4ksu")
    
    if not os.path.exists(ksu_dir):
        run_command(f"git clone -b {ksu_branch} https://github.com/KernelSU-Next/KernelSU-Next.git", cwd=work_dir)
    if not os.path.exists(susfs_dir):
        run_command(f"git clone -b {susfs_branch} https://gitlab.com/simonpunk/susfs4ksu.git", cwd=work_dir)
    
    return ksu_dir, susfs_dir

def apply_patch(patch_file, target_dir):
    """Apply a patch and return a list of generated .rej files."""
    try:
        run_command(f"patch -p1 < {patch_file}", cwd=target_dir)
    except subprocess.CalledProcessError:
        print("Patch application failed, processing reject files.")
    
    rej_files = []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.endswith(".rej"):
                rej_files.append(os.path.join(root, file))
    return rej_files

def parse_reject_file(rej_file):
    """Parse a .rej file and extract failed hunks."""
    with open(rej_file, 'r') as f:
        content = f.read()
    
    lines = content.splitlines()
    print(f"Debug: First 5 lines of {rej_file}:")
    for i, line in enumerate(lines[:5]):
        print(f"Line {i+1}: {line}")
    
    hunks = []
    current_hunk = None
    i = 0
    while i < len(lines):
        if lines[i].startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"context": [], "changes": [], "file": None, "start_line": None}
            match = re.match(r"@@ -(\d+),(\d+)", lines[i])
            if match:
                current_hunk["start_line"] = int(match.group(1))
                current_hunk["end_line"] = int(match.group(1)) + int(match.group(2)) - 1
            if i > 0 and lines[i-1].startswith("--- "):
                current_hunk["file"] = lines[i-1].split()[1]
            elif i > 1 and lines[i-2].startswith("--- "):
                current_hunk["file"] = lines[i-2].split()[1]
            i += 1
        elif lines[i].startswith("***************"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"context": [], "changes": [], "file": None, "start_line": None}
            i += 1
            if i < len(lines) and lines[i].startswith("***"):
                match = re.match(r"\*\*\* (\d+),(\d+)", lines[i])
                if match:
                    current_hunk["start_line"] = int(match.group(1))
                    current_hunk["end_line"] = int(match.group(2))
                if i > 0 and lines[i-2].startswith("--- "):
                    current_hunk["file"] = lines[i-2].split()[1]
            i += 1
        elif current_hunk:
            if lines[i].startswith("- ") or lines[i].startswith("+ "):
                current_hunk["changes"].append(lines[i])
            else:
                current_hunk["context"].append(lines[i].strip())
            i += 1
        else:
            i += 1
    
    if current_hunk:
        hunks.append(current_hunk)
    
    if hunks and not any(hunk["file"] for hunk in hunks):
        derived_file = os.path.basename(rej_file).replace(".rej", "")
        print(f"Warning: No file path found in {rej_file}, assuming {derived_file}")
        for hunk in hunks:
            hunk["file"] = derived_file
    
    return hunks

def find_function_boundary(source_lines, target_line):
    """Find the nearest function or block boundary before the target line."""
    for i in range(target_line - 1, -1, -1):
        line = source_lines[i].strip()
        if line.startswith("static ") or line.startswith("void ") or line.startswith("int ") or line.endswith("{"):
            return i + 1
    return target_line

def apply_hunk_to_file(source_file, hunk, output_file):
    """Apply a hunk to the source file with advanced fuzzy matching and fallback."""
    if not os.path.exists(source_file):
        print(f"Source file {source_file} not found, skipping hunk.")
        return False
    
    with open(source_file, 'r') as f:
        source_lines = f.readlines()
    
    context_lines = [line.strip() for line in hunk["context"] if line.strip()]
    print(f"Debug: Context lines for {source_file}: {context_lines}")
    
    # Step 1: Try exact context matching
    matcher = difflib.SequenceMatcher(None, context_lines, [line.strip() for line in source_lines])
    match = matcher.find_longest_match(0, len(context_lines), 0, len(source_lines))
    
    if context_lines and match.size >= len(context_lines) // 4:
        print(f"Debug: Found context match at line {match.b} with {match.size} matching lines")
        target_line = match.b
        new_lines = source_lines[:target_line]
        
        for change in hunk["changes"]:
            if change.startswith("+ "):
                new_lines.append(change[2:] + "\n")
            elif change.startswith("- "):
                target_line += 1
        
        new_lines.extend(source_lines[target_line:])
    else:
        # Step 2: Try fuzzy matching within a window around start_line
        if hunk["start_line"] is not None:
            window_size = 50
            start = max(0, hunk["start_line"] - 1 - window_size)
            end = min(len(source_lines), hunk["start_line"] - 1 + window_size)
            window_lines = [line.strip() for line in source_lines[start:end]]
            
            matcher = difflib.SequenceMatcher(None, context_lines, window_lines)
            match = matcher.find_longest_match(0, len(context_lines), 0, len(window_lines))
            
            if context_lines and match.size >= 1:  # Allow even a single matching line
                print(f"Debug: Found fuzzy match at line {start + match.b} with {match.size} matching lines")
                target_line = start + match.b
                new_lines = source_lines[:target_line]
                
                for change in hunk["changes"]:
                    if change.startswith("+ "):
                        new_lines.append(change[2:] + "\n")
                    elif change.startswith("- "):
                        target_line += 1
                
                new_lines.extend(source_lines[target_line:])
            else:
                # Step 3: Fallback to inserting at function boundary or start_line
                print(f"Warning: No context match for {source_file}, falling back to insertion")
                target_line = find_function_boundary(source_lines, hunk["start_line"] or 1)
                print(f"Debug: Inserting at line {target_line} (nearest function/block boundary)")
                new_lines = source_lines[:target_line]
                
                for change in hunk["changes"]:
                    if change.startswith("+ "):
                        new_lines.append(change[2:] + "\n")
                
                new_lines.extend(source_lines[target_line:])
        else:
            print(f"No start line available for {source_file}, skipping hunk.")
            return False
    
    with open(output_file, 'w') as f:
        f.writelines(new_lines)
    
    print(f"Debug: Applied changes to {output_file}")
    return True

def generate_new_patch(original_file, modified_file, output_patch):
    """Generate a new patch by comparing original and modified files."""
    if not os.path.exists(original_file):
        print(f"Original file {original_file} not found, cannot generate patch.")
        return
    if not os.path.exists(modified_file):
        print(f"Modified file {modified_file} not found, cannot generate patch.")
        return
    run_command(f"diff -u {original_file} {modified_file} > {output_patch}")

def process_rejects(rej_files, ksu_dir, output_dir):
    """Process all reject files and generate fixed patches."""
    os.makedirs(output_dir, exist_ok=True)
    
    for rej_file in rej_files:
        print(f"Processing reject file: {rej_file}")
        hunks = parse_reject_file(rej_file)
        if not hunks or not any(hunk["file"] for hunk in hunks):
            print(f"No valid hunks or file path found in {rej_file}, skipping.")
            continue
        
        target_file = hunks[0]["file"]
        source_file = os.path.join(ksu_dir, target_file)
        copy_file = os.path.join(output_dir, os.path.basename(target_file) + ".copy")
        output_file = os.path.join(output_dir, os.path.basename(target_file))
        
        if not os.path.exists(source_file):
            print(f"Source file {source_file} not found in KernelSU-Next, skipping.")
            continue
        
        shutil.copyfile(source_file, copy_file)
        
        success = True
        for hunk in hunks:
            if not apply_hunk_to_file(source_file, hunk, output_file):
                success = False
                print(f"Failed to apply hunk in {rej_file}")
        
        if success:
            patch_name = os.path.basename(target_file).replace(".c", ".patch")
            output_patch = os.path.join(output_dir, patch_name)
            generate_new_patch(copy_file, output_file, output_patch)
            print(f"Generated fixed patch: {output_patch}")
        else:
            print(f"Skipping patch generation for {rej_file} due to application failures.")

def main():
    parser = argparse.ArgumentParser(description="Automate kernel patch reject fixing.")
    parser.add_argument("--ksu-branch", default="next", help="KernelSU-Next branch (e.g., next)")
    parser.add_argument("--susfs-branch", default="gki-android13-5.15", help="susfs4ksu branch (e.g., gki-android13-5.15)")
    parser.add_argument("--repo-dir", default="/path/to/kernel_patches", help="Path to kernel_patches repo")
    parser.add_argument("--process-rejects-only", type=lambda x: x.lower() == 'true', default=False, help="Process only existing rejects (true/false)")
    args = parser.parse_args()
    
    repo_dir = args.repo_dir
    rejects_dir = os.path.join(repo_dir, "reject_patcher", "rejects")
    output_dir = os.path.join(repo_dir, "reject_patcher", "output")
    work_dir = os.path.join(repo_dir, "work")
    
    ksu_dir, susfs_dir = clone_repos(args.ksu_branch, args.susfs_branch, work_dir)
    
    rej_files = []
    if not args.process_rejects_only:
        patch_file = os.path.join(ksu_dir, "10_enable_susfs_for_ksu.patch")
        if not os.path.exists(patch_file):
            print(f"Patch file {patch_file} not found, skipping patch application.")
        else:
            rej_files.extend(apply_patch(patch_file, ksu_dir))
    
    for rej_file in Path(rejects_dir).glob("*.rej"):
        rej_files.append(str(rej_file))
    
    if not rej_files:
        print("No reject files found to process.")
        return
    
    process_rejects(rej_files, ksu_dir, output_dir)

if __name__ == "__main__":
    main()
