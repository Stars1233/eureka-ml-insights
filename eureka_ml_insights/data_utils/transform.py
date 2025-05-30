import ast
import logging
import re
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np
import pandas as pd
import tiktoken

from eureka_ml_insights.configs.config import ModelConfig
from eureka_ml_insights.models import (
    AzureOpenAIModel,
    AzureOpenAIOModel,
    ClaudeModel,
    ClaudeReasoningModel,
    DeepseekR1ServerlessAzureRestEndpointModel,
    DirectOpenAIModel,
    DirectOpenAIOModel,
    GeminiModel,
    LlamaServerlessAzureRestEndpointModel,
    MistralServerlessAzureRestEndpointModel,
    TogetherModel,
)


@dataclass
class DFTransformBase:
    @abstractmethod
    def transform(self, df: pd.DataFrame) -> pd.DataFrame: ...


@dataclass
class SequenceTransform(DFTransformBase):
    transforms: List[DFTransformBase]

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        for transform in self.transforms:
            df = transform.transform(df)
        return df


@dataclass
class ColumnRename(DFTransformBase):
    name_mapping: Dict[str, str]

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns=self.name_mapping)


@dataclass
class RunPythonTransform(DFTransformBase):
    """Runs arbitrary python code on the data frame.
    args:
        python_code: str: The python code to run on the data frame.
        global_imports: list: A list of modules to import in global scope, if needed. Default is an empty list.
                       Local (to the python_code scope) imports can be included in the python_code. Such global
                       scope imports are needed when the python_code uses a lambda function, for example, since
                       imports in the python_code scope are not available to the lambda function.
    returns:
        df: pd.DataFrame: The transformed data frame.
    """

    python_code: str
    global_imports: list = field(default_factory=list)

    def __post_init__(self):
        # To avoid disastrous consequences, we only allow operations on the data frame.
        # Therefore, every statement in the python_code should be in the form of df['column_name'] = ... or df = ...
        self.allowed_statement_prefixes = ["df = ", "df[", "import "]
        # Similarly, we only allow a limited set of imports. To add to this safe list, create a PR.
        self.allowed_imports = ["ast", "math", "numpy"]
        self.validate()

    def validate(self):
        # First, splits the python code into python statements and strips whitespace.
        statements = [s.strip() for s in self.python_code.split(";")]
        # Checks that each statement starts with an allowed prefix.
        for statement in statements:
            if not any(statement.startswith(prefix) for prefix in self.allowed_statement_prefixes):
                raise ValueError("For security reasons, only imports and operations on the data frame are allowed.")
        if self.global_imports:
            for module_name in self.global_imports:
                if module_name not in self.allowed_imports:
                    raise ValueError(f"Importing {module_name} in RunPythonTransform is not allowed.")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        # Adds 'df' to the global scope of exec so that it can be overwritten during exec if needed.
        exec_globals = {"df": df}
        exec_globals.update(globals())

        # Adds safe imports to exec_globals.
        for module_name in self.global_imports:
            exec_globals[module_name] = __import__(module_name)

        exec(self.python_code, exec_globals)

        return exec_globals["df"]


@dataclass
class SamplerTransform(DFTransformBase):
    random_seed: int
    sample_count: int
    stratify_by: List[str] = None

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.stratify_by:
            return df.groupby(self.stratify_by, group_keys=False).apply(
                lambda x: x.sample(n=self.sample_count, random_state=self.random_seed)
            )
        else:
            return df.sample(n=self.sample_count, random_state=self.random_seed)


@dataclass
class MultiplyTransform(DFTransformBase):
    """
    Repeats each row n times, and adds a column to the data frame indicating the repeat number.
    Also adds a column to the data frame indicating the data point id that will be the same for
    all repeats of the same data point.
    """

    n_repeats: int

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        dfs = []
        for i in range(self.n_repeats):
            df_copy = df.copy()
            df_copy["data_repeat_id"] = f"repeat_{i}"
            df_copy["data_point_id"] = df_copy.index
            dfs.append(df_copy)
        return pd.concat(dfs, ignore_index=True)


@dataclass
class AddColumn(DFTransformBase):
    column_name: str

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        df[self.column_name] = ""
        return df


@dataclass
class AddColumnAndData(DFTransformBase):
    column_name: str
    data: str

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        df[self.column_name] = str(self.data)

        return df


@dataclass
class CopyColumn(DFTransformBase):
    """Copy a src column's data to a new dst names column."""

    column_name_src: str
    column_name_dst: str

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        df[self.column_name_dst] = df[self.column_name_src]

        return df


@dataclass
class MultiColumnTransform(DFTransformBase):
    """
    Transform class to apply a function to multiple columns.
    This class' validate method checks that the columns to be transformed are present in the data frame,
    and its transform method applies the _transform method to each column.

    This class is meant to be subclassed, and the subclass should implement the _transform method.
    """

    columns: List[str] | str

    def validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Check that all columns to be transformed are present actually in the data frame."""
        # if columns is not a list, make it a list
        if not isinstance(self.columns, list):
            self.columns = [self.columns]
        extra_columns = set(self.columns) - set(df.columns)
        if extra_columns:
            msg = ", ".join(sorted(extra_columns))
            raise ValueError(f"The following columns are not present in the data frame: {msg}")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply the transform to the columns."""
        if df.empty:
            logging.warn("The input dataframe is empty, no transformation was applied.")
            return df
        self.validate(df)
        for column in self.columns:
            df[column] = df[column].apply(self._transform)
        return df


@dataclass
class ShuffleColumnsTransform(MultiColumnTransform):
    """
    For a set of columns, shuffles the values across each row of these columns.
    Values will be shuffled differently for each row.

    This class is meant to be used in MCQ benchmarks to shuffle answer choices
    across different letter options (e.g. shuffle what choice maps to 'A' vs 'B' vs 'C').
    args:
        columns: List[str]: the list of columns from the pandas frame to be reshuffled.
        rng: np.random.Generator: the dedicated numpy generator for the shuffling.
    """

    columns: List[str]
    rng: np.random.Generator = np.random.default_rng(0)

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """For each row in df, shuffle values across these columns."""
        self.validate(df)

        def shuffle_row(row):
            row[self.columns] = self.rng.permutation(row[self.columns].values)
            return row

        df = df.apply(shuffle_row, axis=1)
        return df


@dataclass
class ColumnMatchMapTransform(DFTransformBase):
    """
    Creates a new column indicating the name of the column that matches the value in the key column for each row.
    E.g. for a row, if value of key_col matches value of 'A' column, new_col will contain the value 'A'.
    Used to store the letter of the correct answer choice in MCQ benchmarks.
    """

    key_col: str
    new_col: str
    columns: List[str]

    # Function to find matching column
    def _find_matching_column(self, row):
        for col in self.columns:
            if row[col] == row[self.key_col]:
                return col
        return None  # If no match is found (optional)

    def validate(self, df: pd.DataFrame):
        """Check that all columns to be transformed are present actually in the data frame."""
        extra_columns = set(self.columns + [self.key_col]) - set(df.columns)
        if extra_columns:
            msg = ", ".join(sorted(extra_columns))
            raise ValueError(f"The following columns are not present in the data frame: {msg}")

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """For each row in df, shuffle values across these columns."""
        self.validate(df)
        df[self.new_col] = df.apply(self._find_matching_column, axis=1)
        return df


@dataclass
class ImputeNA(MultiColumnTransform):
    """Impute missing values in selected columns with a specified value."""

    columns: List[str] | str
    value: str

    def _transform(self, value):
        isna = pd.isna(value)
        if isinstance(isna, bool) and isna:
            return self.value
        return value


@dataclass
class ReplaceStringsTransform(MultiColumnTransform):
    """
    Replaces strings in selected columns.  Useful for adhoc fixes, i.e., \\n to \n.
    """

    columns: List[str] | str
    mapping: Dict[str, str]
    case: bool

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        self.validate(df)
        for column in self.columns:
            for source, target in self.mapping.items():
                df[column] = df[column].str.replace(source, target, case=self.case, regex=False)

        return df


@dataclass
class MapStringsTransform(MultiColumnTransform):
    """
    Map values in certain columns of a pandas dataframe according to a mapping dictionary.
    """

    columns: List[str] | str
    mapping: Dict[str, str]

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        self.validate(df)
        for column in self.columns:
            df[column] = df[column].map(self.mapping)

        return df


@dataclass
class PrependStringTransform(MultiColumnTransform):
    """
    Prepends a string for selected columns.
    args:
        columns: List of str or str, Column(s) to apply transform to.
        string: str, string to prepend
    """

    columns: List[str] | str
    string: str

    def _transform(self, value):
        if isinstance(value, list):
            value = [self.string + val for val in value]
        else:
            value = self.string + value
        return value


@dataclass
class RegexTransform(MultiColumnTransform):
    """
    Find the occurrence of the pattern in selected columns.
    args:
        columns: List of str or str, Column(s) to apply transform to.
        prompt_pattern: str, pattern to search for
        ignore_case: bool, True if the regex match should ignore the case
        occurrence: str, "last" or "first" to indicate which of the occurrences to pick
    """

    columns: List[str] | str
    prompt_pattern: str
    ignore_case: bool = False
    occurrence: str = "last"

    def _transform(self, sentence):
        if self.ignore_case:
            results = re.findall(self.prompt_pattern, sentence, flags=re.IGNORECASE)
        else:
            results = re.findall(self.prompt_pattern, sentence)
        if results:
            if self.occurrence == "first":
                return results[0]
            elif self.occurrence == "last":
                return results[len(results) - 1]
        else:
            return None


@dataclass
class ASTEvalTransform(MultiColumnTransform):
    """
    Applies ast.literal_eval to parse strings in selected columns
    """

    columns: List[str] | str

    def _transform(self, string):
        list_strings = ast.literal_eval(string)
        return list_strings


@dataclass
class TokenCounterTransform(MultiColumnTransform):
    """
    Counts the number of tokens in the selected columns.
    """

    columns: List[str] | str

    def transform(self, df: pd.DataFrame, encoding="cl100k_base") -> pd.DataFrame:
        """
        This method uses tiktoken tokenizer to count the number of tokens in the response.
        See: https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
        args:
            df (dataframe): the dataframe to add the token count column to.
            encoding (str): the encoding to use with tiktoken. Default is "cl100k_base".
        returns:
            dataframe: the dataframe with the token count column added.
        """
        self.validate(df)
        encoding = tiktoken.get_encoding(encoding)
        for column in self.columns:
            token_count = df[column].apply(lambda x: len(encoding.encode(x)))
            token_count_column = f"{column}_token_count"
            df[token_count_column] = token_count
        return df


@dataclass
class MajorityVoteTransform:
    """Applies the majority vote transformation to the specified model output column per id_col.
    If model_label_column is provided, the corresponding label or score of the majority vote output will also be added.
    """

    model_output_col: str = "model_output"  # Default column name for model outputs
    model_label_column: str = None  # Column name for model labels or scores corresponding to model outputs
    id_col: str = "data_point_id"  # Default column name for IDs
    majority_vote_col: str = "majority_vote"  # Default column name for storing majority vote
    majority_label_col: str = (
        "majority_label"  # Default column name for storing label corresponding to majority vote output
    )

    def transform(self, df: pd.DataFrame, random_state: int = 0) -> pd.DataFrame:
        """
        Transforms the dataframe by calculating the majority vote of model_output_col per id_col.
        If the 'model_output' is NaN, it will be droped before calculating the majority vote.

        Args:
            df (pd.DataFrame): Input dataframe containing model_output_col and id_col.
            random_state (int): Input random seed

        Returns:
            pd.DataFrame: Transformed dataframe with majority vote for each id_col.
        """
        result_df = df.groupby(self.id_col).apply(
            self.majority_vote,
            self.model_output_col,
            self.model_label_column,
            self.majority_vote_col,
            self.majority_label_col,
            random_state=random_state,
        )
        return result_df

    @staticmethod
    def majority_vote(
        group, model_output_col, model_label_col, majority_vote_col, majority_label_col, random_state: int = 0
    ):
        """
        Calculate majority vote for each group.
        Args:
            group (pd.DataFrame): Input dataframe containing model_output_col, id_col and label.
            model_output_col (str): Model output column name
            model_label_col (str): Model label column name
            majority_vote_col (str): Majority vote column name
            majority_label_col (str): Majority label column name
        Returns:
            pd.DataFrame: Transformed dataframe with majority vote for each id_col.
        """
        x = group[model_output_col]
        majority_value = (
            x.dropna().mode().sample(n=1, random_state=random_state).iloc[0] if not x.dropna().mode().empty else pd.NA
        )
        group[majority_vote_col] = majority_value
        if model_label_col:
            group[majority_label_col] = group.loc[group[model_output_col] == majority_value, model_label_col].iloc[0]
        return group


@dataclass
class ExtractUsageTransform:
    """
    Extracts token usage completion numbers (except prompt input tokens) for all models.
    args:
        model_config: config used for the experiment.
        usage_completion_output_col: str, default name of the column where completion numbers will be stored for model
        usage_column: str, default name of the column where usage information is stored for model
        n_tokens_column: str, default name of the column where number of tokens is stored for model
    """

    model_config: ModelConfig
    usage_completion_output_col: str = "usage_completion"
    usage_column: str = "usage"
    n_tokens_column: str = "n_output_tokens"

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Transforms the dataframe by extracting the .

        Args:
            df (pd.DataFrame): Input dataframe of inference results retrieved with the model_config.

        Returns:
            pd.DataFrame: Transformed dataframe with completion token numbers in usage_completion_output_col.
        """
        usage_completion_read_col = None
        if self.model_config.class_name is GeminiModel:
            usage_completion_read_col = "candidates_token_count"
        elif self.model_config.class_name is ClaudeModel or self.model_config.class_name is ClaudeReasoningModel:
            usage_completion_read_col = "output_tokens"
        elif (
            self.model_config.class_name is AzureOpenAIOModel
            or self.model_config.class_name is AzureOpenAIModel
            or self.model_config.class_name is LlamaServerlessAzureRestEndpointModel
            or self.model_config.class_name is MistralServerlessAzureRestEndpointModel
            or self.model_config.class_name is DeepseekR1ServerlessAzureRestEndpointModel
            or self.model_config.class_name is DirectOpenAIModel
            or self.model_config.class_name is DirectOpenAIOModel
            or self.model_config.class_name is TogetherModel
        ):
            usage_completion_read_col = "completion_tokens"
        else:
            logging.warn(
                f"Model {self.model_config.class_name} is not recognized for extracting completion token usage."
            )
        # if the model is one for which the usage of completion tokens is known, use that corresponding column for the model
        # otherwise, use the default "n_output_tokens" which is computed with a universal tokenizer as shown in TokenCounterTransform()
        self.validate(df, usage_completion_read_col)
        if usage_completion_read_col:
            df[self.usage_completion_output_col] = df.apply(
                lambda x: self._extract_usage(x, usage_completion_read_col), axis=1
            )
        elif self.n_tokens_column in df.columns:
            df[self.usage_completion_output_col] = df[self.n_tokens_column]
        else:
            df[self.usage_completion_output_col] = np.nan
        return df

    def validate(self, df: pd.DataFrame, usage_completion_read_col: str) -> pd.DataFrame:
        """Check that usage_columns or n_tokens_columns are present actually in the data frame.
        Args:
            df (pd.DataFrame): Input dataframe containing model_output_col and id_col.
            usage_completion_read_col (str): The column name for token extraction.
        """
        if usage_completion_read_col and self.usage_column not in df.columns:
            raise ValueError(f"The {self.usage_column} column is not present in the data frame.")
        elif self.n_tokens_column not in df.columns:
            raise ValueError(f"The {self.n_tokens_column} column is not present in the data frame.")

    def _extract_usage(self, row, usage_completion_read_col):
        """
        Extracts the token usage for a given row if usage column and corresponding completion column exists.
        Args:
            row (pd.Series): A row of the dataframe.
            usage_completion_read_col (str): The column name to extract the token usage from.
        Returns:
            int: The token usage for the row.
        """
        if not pd.isna(row[self.usage_column]) and usage_completion_read_col in row[self.usage_column]:
            return row[self.usage_column][usage_completion_read_col]
        return np.nan
