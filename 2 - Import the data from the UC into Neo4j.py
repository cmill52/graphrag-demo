# Databricks notebook source
# MAGIC %md
# MAGIC # Building a Knowledge Graph from Delta Tables
# MAGIC
# MAGIC Before building the agent, we need to set up the graph database. To implement the knowledge graphs (KG) for our graph database, it's essential to first consider the nature of the source data. The key question here is: "Is the data structured or unstructured?" 
# MAGIC This post focuses largely on building a KG with structured data, however, if your data is unstructured, there’s generally much more work involved to build a KG. Generally, one can use LLMs to extract structured information from unstructured content, being thoughtful about how best to extract and represent entities, attributes, and relationships in that data. More advanced approaches involve multi-step methods, including [this approach from Microsoft Research](https://arxiv.org/abs/2404.16130), but this remains, as of early 2025, an area of active research. 
# MAGIC For structured data, a useful starting point is to leverage the structural dependencies already present in your Lakehouse’s silver tables; these tables are typically defined with constraints like foreign keys. 
# MAGIC
# MAGIC For structured data, a useful starting point is to leverage the structural dependencies already present in your lakehouse's silver tables; these tables are typically defined with constraints like **foreign keys**.
# MAGIC
# MAGIC A simple example would be the following E-R diagram of the BloodHound example
# MAGIC
# MAGIC <img src="images/er-model.png">
# MAGIC
# MAGIC which describes computers that are members of active directory groups and that can be translated in the following graph structure:
# MAGIC
# MAGIC <img src="images/er-to-graph-model.png">
# MAGIC
# MAGIC In this particular scenario, with the Bloodhound dataset, the final graph model will look like the following:
# MAGIC
# MAGIC <img src="https://guides.neo4j.com/sandbox/cybersecurity/img/model.svg">

# COMMAND ----------

# MAGIC %md
# MAGIC ## How to create a Neo4j instance
# MAGIC You can create a free Neo4j Aura instance from [here](https://neo4j.com/product/auradb/).
# MAGIC The free version is just enough for the scope of this demo and once you got the endpoints put them in the `.env` files in the `NEO4J_URL` and `NEO4J_PASSWORD` variables.

# COMMAND ----------

# MAGIC %pip install -q python-dotenv
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %run ./Config

# COMMAND ----------

import os
import pyspark.sql.functions as F
from pyspark.sql import SparkSession
from dotenv import load_dotenv

# COMMAND ----------

load_dotenv()

# COMMAND ----------

spark = (
    SparkSession.builder.config("neo4j.url", url)
    .config("neo4j.authentication.basic.username", username)
    .config("neo4j.authentication.basic.password", password)
    .config("neo4j.database", dbname)
    .getOrCreate()
)

# COMMAND ----------

# Query to get the list of tables in the specified catalog and database
nodes = (
  spark
    .sql(f'show tables in {catalog_name}.default')
    # Filter the tables to include only those in the node_tables list
    .where(F.col('tableName').isin(list(node_tables)))
    # Select only the tableName column
    .select('tableName')
    # Collect the results into a list of Row objects
    .collect()
)

# COMMAND ----------

for node in nodes:
  # Extract the primary key column name for the current table
  table_pk = ( 
    spark
      .sql(f'describe table extended {catalog_name}.default.`{node.tableName}`')
      .where("col_name like '%_pk'")
      .select(F.regexp_extract_all(F.col('data_type'), F.lit('PRIMARY KEY \\(`(.*)`\\)'), F.lit(1)).getItem(0).alias('pk'))
      .collect()[0]
      .pk
  )
  
  # Read the table data and write it to Neo4j with the appropriate options
  (
    spark.read
      .table(f'{catalog_name}.default.`{node.tableName}`')
      .write
      .mode("overwrite")
      .format("org.neo4j.spark.DataSource")
      .option("labels", node.tableName)
      .option("node.keys", table_pk)
      .option("schema.optimization.node.keys", "UNIQUE")
      .save()
  )

# COMMAND ----------

# Query to get the list of tables in the specified catalog and database
formatted_rel_tables = [s.replace('-', '_') for s in rel_tables]

relationships = (
  spark
    .sql(f'show tables in {catalog_name}.default')
    .where(F.col('tableName').isin(list(formatted_rel_tables)))
    .select('tableName')
    .collect()
)

# COMMAND ----------

# Define the regular expression pattern for extracting foreign key information
# Define the regular expression pattern for extracting foreign key information
regexpr = r"FOREIGN\s+KEY\s*\(`([^`]+)`\)\s*REFERENCES\s*(?:`[^`]+`\.)?(?:`[^`]+`\.)?`([^`]+)`\s*\(`([^`]+)`\)"
datatype_col = F.col('data_type')

# Iterate over each relationship table
for rel in relationships:
  # Extract foreign key information for the current table
  table_fks = ( 
    spark
      .sql(f'describe table extended {catalog_name}.dbx_genai_bloodhound_demo.`{rel.tableName}`')
      .where("col_name like '%_fk'")
      .select(
        F.regexp_extract('data_type', regexpr, 1).alias('table_col'),
        F.regexp_extract('data_type', regexpr, 2).alias('fk_table_name'),
        F.regexp_extract('data_type', regexpr, 3).alias('fk_table_col')
    )
      .collect()
  )
  
  if table_fks:
    # Extract the relationship name from the table name
    rel_name = rel.tableName.split("_")[0]
    
    # Initialize variables for source and target labels and keys
    for table_fk in table_fks:
      if table_fk.table_col.startswith('source_'):
        source_label = table_fk.fk_table_name
        source_key = f'{table_fk.table_col}:{table_fk.fk_table_col}'

        (
        spark.read
          .table(f'{catalog_name}.dbx_genai_bloodhound_demo.`{rel.tableName}`')
          .coalesce(1)
          .write
          .mode("overwrite")
          .format("org.neo4j.spark.DataSource")
          .option("relationship", rel_name.upper())
          .option("relationship.save.strategy", "keys")
          .option("relationship.source.labels", source_label)
          .option("relationship.source.save.mode", "Match")
          .option("relationship.source.node.keys", source_key)
          .save()
        )
      else:
        target_label = table_fk.fk_table_name
        target_key = f'{table_fk.table_col}:{table_fk.fk_table_col}'

        (
        spark.read
          .table(f'{catalog_name}.dbx_genai_bloodhound_demo.`{rel.tableName}`')
          .coalesce(1)
          .write
          .mode("overwrite")
          .format("org.neo4j.spark.DataSource")
          .option("relationship", rel_name.upper())
          .option("relationship.save.strategy", "keys")
          .option("relationship.target.labels", target_label)
          .option("relationship.target.save.mode", "Match")
          .option("relationship.target.node.keys", target_key)
          .save()
        ) 
  else:
    print(f'No foreign keys found for table {rel.tableName}')
    continue