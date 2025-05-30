import os

"""This file contains user defined configuration classes for the GPQA dataset."""

from typing import Any

import numpy as np

from eureka_ml_insights.configs import (
    AggregatorConfig,
    DataProcessingConfig,
    DataSetConfig,
    EvalReportingConfig,
    ExperimentConfig,
    InferenceConfig,
    MetricConfig,
    ModelConfig,
    PipelineConfig,
    PromptProcessingConfig,
)
from eureka_ml_insights.configs.model_configs import (
    OAI_GPT4O_2024_11_20_CONFIG,
)
from eureka_ml_insights.core import (
    DataProcessing,
    EvalReporting,
    Inference,
    PromptProcessing,
)
from eureka_ml_insights.data_utils import (
    ColumnMatchMapTransform,
    ColumnRename,
    CopyColumn,
    DataReader,
    ExtractUsageTransform,
    HFDataReader,
    ImputeNA,
    MajorityVoteTransform,
    MMDataLoader,
    MultiplyTransform,
    RegexTransform,
    ReplaceStringsTransform,
    RunPythonTransform,
    SequenceTransform,
    ShuffleColumnsTransform,
)
from eureka_ml_insights.metrics import (
    BiLevelAggregator,
    BiLevelCountAggregator,
    CountAggregator,
    ExactMatch,
)

from .llm_extraction import LLM_EXTRACTION_SUBPIPELINE_MIXIN

class GPQA_Experiment_Pipeline(ExperimentConfig):
    def configure_pipeline(
        self, model_config: ModelConfig, resume_from: str = None, **kwargs: dict[str, Any]
    ) -> PipelineConfig:
        rng = np.random.default_rng(42)
        # Configure the data processing component.
        self.data_processing_comp = PromptProcessingConfig(
            component_type=PromptProcessing,
            data_reader_config=DataSetConfig(
                HFDataReader,
                {
                    "path": "Idavidrein/gpqa",
                    "tasks": "gpqa_diamond",
                    "split": "train",
                    "transform": SequenceTransform(
                        [
                            CopyColumn(column_name_src="Correct Answer", column_name_dst="A"),
                            CopyColumn(column_name_src="Incorrect Answer 1", column_name_dst="B"),
                            CopyColumn(column_name_src="Incorrect Answer 2", column_name_dst="C"),
                            CopyColumn(column_name_src="Incorrect Answer 3", column_name_dst="D"),
                            ShuffleColumnsTransform(columns=["A", "B", "C", "D"], rng=rng),
                            # finds answer choice that "Correct Answer" is mapped to, and stores it in "ground_truth"
                            ColumnMatchMapTransform(
                                new_col="ground_truth", key_col="Correct Answer", columns=["A", "B", "C", "D"]
                            ),
                            MultiplyTransform(n_repeats=1),
                        ]
                    ),
                },
            ),
            prompt_template_path=os.path.join(
                os.path.dirname(__file__),
                "../prompt_templates/gpqa_templates/gpqa_cot.jinja",
            ),
            output_dir=os.path.join(self.log_dir, "data_processing_output"),
        )
        # Configure the inference component
        self.inference_comp = InferenceConfig(
            component_type=Inference,
            model_config=model_config,
            data_loader_config=DataSetConfig(
                MMDataLoader,
                {"path": os.path.join(self.data_processing_comp.output_dir, "transformed_data.jsonl")},
            ),
            output_dir=os.path.join(self.log_dir, "inference_result"),
            resume_from=resume_from,
            max_concurrent=1,
        )
        self.preeval_data_post_processing_comp = DataProcessingConfig(
            component_type=DataProcessing,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.inference_comp.output_dir, "inference_result.jsonl"),
                    "format": ".jsonl",
                    "transform": SequenceTransform(
                        [
                            # run a transformation to get the total token count used by the model for completion only (except prompt input tokens)
                            # this is needed because different models use different fields to indicate this
                            ExtractUsageTransform(model_config),
                            CopyColumn(
                                column_name_src="model_output",
                                column_name_dst="raw_model_output",
                            ),
                            RegexTransform(
                                columns="model_output",
                                prompt_pattern=r"Final Answer: (\w)(?=\s|\W|$)",
                                ignore_case=False,
                            ),
                            ImputeNA(columns="model_output", value=""),
                        ]
                    ),
                },
            ),
            output_dir=os.path.join(self.log_dir, "preeval_data_post_processing_output"),
        )
        # Inserts an llm-based extraction subpipeline that filters all rows where the regex answer extraction did not work, 
        # and attempts answer extraction using an LLM. This is helpful for cases where:
        # the model under test may not completely follow the instructions in the prompt on how to mark the final answer,
        # or for reasoning models that may run out of the max token length and may not be able to add the final answer marker.
        self.llm_extraction_subpipeline_conf = LLM_EXTRACTION_SUBPIPELINE_MIXIN()
        self.llm_extraction_subpipeline = self.llm_extraction_subpipeline_conf.configure_subpipeline(
            extraction_attempt_component=self.preeval_data_post_processing_comp,
            extracted_answer_col="model_output",
            llm_extraction_prompt_template=os.path.join(
                os.path.dirname(__file__),
                "../prompt_templates/gpqa_templates/extract_gpqa_answer.jinja",
            ),
            llm_extractor_model_config=OAI_GPT4O_2024_11_20_CONFIG,
            log_dir=self.log_dir,
            llm_extractor_max_concurrent=1,
            llm_extractor_answer_transforms=[
                RegexTransform(
                    columns="model_output",
                    prompt_pattern=r"Final Answer: (\w)(?=\s|\W|$)",
                    ignore_case=True,
                )
            ],
        )

        # Configure the evaluation and reporting component for pass@1.
        self.evalreporting_comp = EvalReportingConfig(
            component_type=EvalReporting,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.llm_extraction_subpipeline[-1].output_dir, "transformed_data.jsonl"),
                    "format": ".jsonl",
                },
            ),
            metric_config=MetricConfig(ExactMatch),
            aggregator_configs=[
                # the first three reports aggregate the metrics per experiment repeat
                # each repeat can be considered as an individual pass@1 score
                AggregatorConfig(
                    CountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "group_by": "data_repeat_id",
                        "filename_base": "ExactMatch_SeparateRuns",
                        "normalize": True,
                    },
                ),
                AggregatorConfig(
                    CountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "group_by": ["data_repeat_id", "Subdomain"],
                        "filename_base": "ExactMatch_GroupBy_Subdomain_SeparateRuns",
                        "normalize": True,
                    },
                ),
                AggregatorConfig(
                    CountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "group_by": ["data_repeat_id", "High-level domain"],
                        "filename_base": "ExactMatch_GroupBy_High-level_domain_SeparateRuns",
                        "normalize": True,
                    },
                ),
                # the next three reports take the average and std for all repeats
                # the resulting numbers are the average and std of N pass@1 scores, where N is number of repeats
                AggregatorConfig(
                    BiLevelCountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "first_groupby": "data_repeat_id",
                        "filename_base": "ExactMatch_AllRuns",
                        "normalize": True,
                    },
                ),
                AggregatorConfig(
                    BiLevelCountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "first_groupby": ["data_repeat_id", "Subdomain"],
                        "second_groupby": "Subdomain",
                        "filename_base": "ExactMatch_GroupBy_Subdomain_AllRuns",
                        "normalize": True,
                    },
                ),
                AggregatorConfig(
                    BiLevelCountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "first_groupby": ["data_repeat_id", "High-level domain"],
                        "second_groupby": "High-level domain",
                        "filename_base": "ExactMatch_GroupBy_High-level_domain_AllRuns",
                        "normalize": True,
                    },
                ),
                # three similar reports for average completion usage
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["usage_completion"],
                        "first_groupby": "data_repeat_id",
                        "filename_base": "UsageCompletion_AllRuns",
                        "agg_fn": "mean",
                    },
                ),
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["usage_completion"],
                        "first_groupby": ["data_repeat_id", "Subdomain"],
                        "second_groupby": "Subdomain",
                        "filename_base": "UsageCompletion_GroupBy_Subdomain_AllRuns",
                        "agg_fn": "mean",
                    },
                ),
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["usage_completion"],
                        "first_groupby": ["data_repeat_id", "High-level domain"],
                        "second_groupby": "High-level domain",
                        "filename_base": "UsageCompletion_GroupBy_High-level_domain_AllRuns",
                        "agg_fn": "mean",
                    },
                ),
            ],
            output_dir=os.path.join(self.log_dir, "eval_report"),
        )

        self.posteval_data_post_processing_comp = DataProcessingConfig(
            component_type=DataProcessing,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.evalreporting_comp.output_dir, "metric_results.jsonl"),
                    "format": ".jsonl",
                    "transform": SequenceTransform(
                        [
                            CopyColumn(
                                column_name_src="ExactMatch_result",
                                column_name_dst="ExactMatch_result_numeric",
                            ),
                            ReplaceStringsTransform(
                                columns=["ExactMatch_result_numeric"],
                                mapping={"incorrect": "0", "correct": "1", "none": "NaN"},
                                case=False,
                            ),
                        ]
                    ),
                },
            ),
            output_dir=os.path.join(self.log_dir, "posteval_data_post_processing_output"),
        )

        # Aggregate the results by best of n
        # In this case, this is equivalent to taking the max on the numerical column of the metric.
        self.bon_evalreporting_comp = EvalReportingConfig(
            component_type=EvalReporting,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.posteval_data_post_processing_comp.output_dir, "transformed_data.jsonl"),
                    "format": ".jsonl",
                },
            ),
            aggregator_configs=[
                # the first three reports aggregate results by data_point_id and take the best out of N
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["ExactMatch_result_numeric"],
                        "first_groupby": "data_point_id",
                        "filename_base": "ExactMatch_BestOfN",
                        "agg_fn": "max",
                    },
                ),
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["ExactMatch_result_numeric"],
                        "first_groupby": "data_point_id",
                        "second_groupby": "Subdomain",
                        "filename_base": "ExactMatch_BestOfN_GroupBy_Subdomain",
                        "agg_fn": "max",
                    },
                ),
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["ExactMatch_result_numeric"],
                        "first_groupby": "data_point_id",
                        "second_groupby": "High-level domain",
                        "filename_base": "ExactMatch_BestOfN_GroupBy_High-level_domain",
                        "agg_fn": "max",
                    },
                ),
                # aggregates results by data_point_id and takes the sum of usage for completion tokens
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["usage_completion"],
                        "first_groupby": "data_point_id",
                        "filename_base": "UsageCompletion_BestOfN",
                        "agg_fn": "sum",
                    },
                ),
            ],
            output_dir=os.path.join(self.log_dir, "bestofn_eval_report"),
        )

        self.won_evalreporting_comp = EvalReportingConfig(
            component_type=EvalReporting,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.posteval_data_post_processing_comp.output_dir, "transformed_data.jsonl"),
                    "format": ".jsonl",
                },
            ),
            aggregator_configs=[
                # the first three reports aggregate results by data_point_id and take the best out of N
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["ExactMatch_result_numeric"],
                        "first_groupby": "data_point_id",
                        "filename_base": "ExactMatch_WorstOfN",
                        "agg_fn": "min",
                    },
                ),
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["ExactMatch_result_numeric"],
                        "first_groupby": "data_point_id",
                        "second_groupby": "Subdomain",
                        "filename_base": "ExactMatch_WorstOfN_GroupBy_Subdomain",
                        "agg_fn": "min",
                    },
                ),
                AggregatorConfig(
                    BiLevelAggregator,
                    {
                        "column_names": ["ExactMatch_result_numeric"],
                        "first_groupby": "data_point_id",
                        "second_groupby": "High-level domain",
                        "filename_base": "ExactMatch_WorstOfN_GroupBy_High-level_domain",
                        "agg_fn": "min",
                    },
                ),
            ],
            output_dir=os.path.join(self.log_dir, "worstofn_eval_report"),
        )

        # aggregate the output by majority vote
        self.data_post_processing_mv = DataProcessingConfig(
            component_type=DataProcessing,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.evalreporting_comp.output_dir, "metric_results.jsonl"),
                    "format": ".jsonl",
                    "transform": SequenceTransform(
                        [
                            MajorityVoteTransform(model_output_col="model_output"),
                            ColumnRename(
                                name_mapping={
                                    "model_output": "model_output_onerun",
                                    "majority_vote": "model_output",
                                }
                            ),
                            RunPythonTransform("df = df[df['data_repeat_id'] == 'repeat_0']"),
                        ]
                    ),
                },
            ),
            output_dir=os.path.join(self.log_dir, "data_post_processing_mv"),
        )

        # Configure the evaluation and reporting component for majority vote.
        self.mv_evalreporting_comp = EvalReportingConfig(
            component_type=EvalReporting,
            data_reader_config=DataSetConfig(
                DataReader,
                {
                    "path": os.path.join(self.data_post_processing_mv.output_dir, "transformed_data.jsonl"),
                    "format": ".jsonl",
                },
            ),
            metric_config=MetricConfig(ExactMatch),
            aggregator_configs=[
                # these three reports aggregate the metrics for the majority vote results
                AggregatorConfig(
                    CountAggregator,
                    {"column_names": ["ExactMatch_result"], "filename_base": "MajorityVote", "normalize": True},
                ),
                AggregatorConfig(
                    CountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "group_by": ["Subdomain"],
                        "filename_base": "MajorityVote_GroupBy_Subdomain",
                        "normalize": True,
                    },
                ),
                AggregatorConfig(
                    CountAggregator,
                    {
                        "column_names": ["ExactMatch_result"],
                        "group_by": ["High-level domain"],
                        "filename_base": "MajorityVote_GroupBy_High-level_domain",
                        "normalize": True,
                    },
                ),
            ],
            output_dir=os.path.join(self.log_dir, "majorityvote_eval_report"),
        )
        # # Configure the pipeline
        return PipelineConfig(
            [
                self.data_processing_comp,
                self.inference_comp,
                self.preeval_data_post_processing_comp,
            ]
            + self.llm_extraction_subpipeline
            + [
                self.evalreporting_comp,
                self.posteval_data_post_processing_comp,
                self.bon_evalreporting_comp,
                self.won_evalreporting_comp,
                self.data_post_processing_mv,
                self.mv_evalreporting_comp,
            ],
            self.log_dir,
        )


class GPQA_PIPELINE_5Run(GPQA_Experiment_Pipeline):
    """This class specifies the config for running the GPQA benchmark 5 repeated times"""

    def configure_pipeline(
        self, model_config: ModelConfig, resume_from: str = None, **kwargs: dict[str, Any]
    ) -> PipelineConfig:
        pipeline = super().configure_pipeline(model_config=model_config, resume_from=resume_from)
        # data preprocessing
        self.data_processing_comp.data_reader_config.init_args["transform"].transforms.append(
            MultiplyTransform(n_repeats=5)
        )
        return pipeline
