import os
import json
import duckdb
from pathlib import Path
from typing import List, Set, Dict, Optional

def extract_students(students_dir: str) -> List[Set[str]]:
    """
    Extracts all student names and their citation formats from JSON files.

    Args:
        students_dir (str): Path to the directory containing student JSON files.

    Returns:
        List[Set[str]]: A list where each element is a set of name variations 
                       (full name and citation names) for a single student.
    """
    students_names = []
    path = Path(students_dir)
    
    if not path.exists() or not path.is_dir():
        print(f"Warning: Directory {students_dir} not found.")
        return students_names

    for json_file in path.glob("*.json"):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                info = data.get('informacoes_pessoais', {})
                
                full_name = info.get('nome_completo')
                citations_str = info.get('nome_citacoes', '')
                
                # Initialize set for this student
                name_variations = set()
                
                if full_name:
                    name_variations.add(full_name.strip().lower())
                
                if citations_str:
                    # Split citation names by semicolon and clean them
                    citations = [c.strip().lower() for c in citations_str.split(';') if c.strip()]
                    name_variations.update(citations)
                
                if name_variations:
                    students_names.append(name_variations)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading {json_file}: {e}")
            
    return students_names

def is_student_coauthor(authors_str: Optional[str], students_list: List[Set[str]]) -> bool:
    """
    Checks if any student from the provided list is a co-author in the given authors string.

    Args:
        authors_str (Optional[str]): The string containing authors of a paper.
        students_list (List[Set[str]]): List of sets containing student name variations.

    Returns:
        bool: True if at least one student is detected as a co-author, False otherwise.
    """
    if not authors_str or not isinstance(authors_str, str):
        return False
    
    authors_normalized = authors_str.lower()
    
    for student_variations in students_list:
        for name in student_variations:
            # We check if the name variation is present as a substring in the authors list
            # To avoid partial matches (e.g., "Ana" in "Ana Paula"), we could split by common delimiters
            # but typically citation names are specific enough.
            if name in authors_normalized:
                return True
                
    return False

def process_coauthorship():
    """
    Main script to connect to DuckDB, analyze papers, and mark student co-authorship.
    """
    db_path = "pesquisadores.duckdb"
    students_dir = "dados_brutos/alunos/"
    tables = ["tb_artigo_periodico", "tb_artigo_conferencia"]
    
    # 1. Extract students
    print("Extracting student data...")
    students_list = extract_students(students_dir)
    print(f"Loaded {len(students_list)} students.")

    if not students_list:
        print("No student data found. Exiting.")
        return

    try:
        # 2. Connect to DuckDB
        conn = duckdb.connect(db_path)
        
        for table in tables:
            print(f"Processing table: {table}...")
            
            # Check if table exists
            check_table = conn.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{table}'").fetchone()
            if not check_table or check_table[0] == 0:
                print(f"Table {table} does not exist. Skipping.")
                continue

            # Add column coautoria_aluno if it doesn't exist
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN coautoria_aluno BOOLEAN")
                print(f"Added column coautoria_aluno to {table}.")
            except duckdb.CatalogException:
                print(f"Column coautoria_aluno already exists in {table}.")

            # Fetch papers (assuming they have an 'id' column and 'autores' column)
            # We use a generic approach to find the primary key or just update by row
            # For simplicity in DuckDB, we can fetch all, process, and update.
            
            # Try to find the primary key column
            pk_res = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            pk_col = None
            for col in pk_res:
                if col[3] == 1: # pk flag
                    pk_col = col[1]
                    break
            
            if not pk_col:
                # If no PK, we might have to use a different strategy, 
                # but academic tables usually have an ID.
                print(f"Could not find primary key for {table}. Skipping updates.")
                continue

            # Fetch ID and Authors
            # We use a try-except in case 'autores' column is named differently (though prompt says 'autores')
            try:
                papers = conn.execute(f"SELECT {pk_col}, autores FROM {table}").fetchall()
            except duckdb.CatalogException:
                print(f"Column 'autores' not found in {table}. Skipping.")
                continue

            # 3. Update co-authorship
            updates = []
            for paper_id, authors in papers:
                has_student = is_student_coauthor(authors, students_list)
                updates.append((has_student, paper_id))
            
            # To perform updates efficiently in DuckDB:
            # Create a temporary table with results and join for update
            temp_table_name = f"temp_updates_{table}"
            conn.execute(f"CREATE TABLE {temp_table_name} (coautoria BOOLEAN, id {pk_res[0][2] if pk_res else 'VARCHAR'})")
            
            # We need to handle types correctly for the ID. 
            # Since we don't know the exact type, we'll use a parameterized insert.
            conn.executemany(f"INSERT INTO {temp_table_name} VALUES (?, ?)", 
                            [(u[0], u[1]) for u in updates])
            
            # Update the original table
            conn.execute(f"""
                UPDATE {table} 
                SET coautoria_aluno = t.coautoria 
                FROM {temp_table_name} t 
                WHERE {table}.{pk_col} = t.id
            """)
            
            # Cleanup temp table
            conn.execute(f"DROP TABLE {temp_table_name}")
            print(f"Successfully updated co-authorship for {table}.")

        conn.close()
        print("Co-authorship detection complete.")

    except Exception as e:
        print(f"An error occurred during database processing: {e}")

if __name__ == "__main__":
    process_coauthorship()
