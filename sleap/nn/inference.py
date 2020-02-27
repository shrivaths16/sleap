"""Inference pipelines and utilities."""

import os
from collections import defaultdict

import tensorflow as tf

import attr
from typing import Text, Optional, List, Dict

import sleap
from sleap import util
from sleap.nn.config import TrainingJobConfig
from sleap.nn.model import Model
from sleap.nn.tracking import Tracker
from sleap.nn.data.pipelines import (
    Provider,
    Pipeline,
    LabelsReader,
    VideoReader,
    Normalizer,
    Resizer,
    Prefetcher,
    KerasModelPredictor,
    LocalPeakFinder,
    PredictedInstanceCropper,
    GlobalPeakFinder,
    KeyFilter,
    PredictedCenterInstanceNormalizer,
    PartAffinityFieldInstanceGrouper,
    PointsRescaler,
)


def group_examples(examples):
    grouped_examples = defaultdict(list)
    for example in examples:
        video_ind = example["video_ind"].numpy()
        frame_ind = example["frame_ind"].numpy()
        grouped_examples[(video_ind, frame_ind)].append(example)
    return grouped_examples


@attr.s(auto_attribs=True)
class TopdownPredictor:
    centroid_config: TrainingJobConfig
    centroid_model: Model
    confmap_config: TrainingJobConfig
    confmap_model: Model
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(
        cls, centroid_model_path: Text, confmap_model_path: Text
    ) -> "TopdownPredictor":
        """Create predictor from saved models."""
        # Load centroid model.
        centroid_config = TrainingJobConfig.load_json(centroid_model_path)
        centroid_keras_model_path = os.path.join(centroid_model_path, "best_model.h5")
        centroid_model = Model.from_config(centroid_config.model)
        centroid_model.keras_model = tf.keras.models.load_model(
            centroid_keras_model_path, compile=False
        )

        # Load confmap model.
        confmap_config = TrainingJobConfig.load_json(confmap_model_path)
        confmap_keras_model_path = os.path.join(confmap_model_path, "best_model.h5")
        confmap_model = Model.from_config(confmap_config.model)
        confmap_model.keras_model = tf.keras.models.load_model(
            confmap_keras_model_path, compile=False
        )

        return cls(
            centroid_config=centroid_config,
            centroid_model=centroid_model,
            confmap_config=confmap_config,
            confmap_model=confmap_model,
        )

    def make_pipeline(self, data_provider: Optional[Provider] = None) -> Pipeline:

        pipeline = Pipeline()
        if data_provider is not None:
            pipeline.providers = [data_provider]

        pipeline += Normalizer.from_config(self.centroid_config.data.preprocessing)
        pipeline += Resizer.from_config(
            self.centroid_config.data.preprocessing,
            keep_full_image=True,
            points_key=None,
        )

        pipeline += Prefetcher()

        pipeline += KerasModelPredictor(
            keras_model=self.centroid_model.keras_model,
            model_input_keys="image",
            model_output_keys="predicted_centroid_confidence_maps",
        )

        pipeline += LocalPeakFinder(
            confmaps_stride=self.centroid_model.heads[0].output_stride,
            peak_threshold=0.2,
            confmaps_key="predicted_centroid_confidence_maps",
            peaks_key="predicted_centroids",
            peak_vals_key="predicted_centroid_confidences",
            peak_sample_inds_key="predicted_centroid_sample_inds",
            peak_channel_inds_key="predicted_centroid_channel_inds",
            keep_confmaps=False,
        )

        pipeline += PredictedInstanceCropper(
            crop_width=self.confmap_config.data.instance_cropping.crop_size,
            crop_height=self.confmap_config.data.instance_cropping.crop_size,
            centroids_key="predicted_centroids",
            centroid_confidences_key="predicted_centroid_confidences",
            full_image_key="full_image",
        )

        pipeline += KerasModelPredictor(
            keras_model=self.confmap_model.keras_model,
            model_input_keys="instance_image",
            model_output_keys="predicted_instance_confidence_maps",
        )
        pipeline += GlobalPeakFinder(
            confmaps_key="predicted_instance_confidence_maps",
            peaks_key="predicted_center_instance_points",
            confmaps_stride=self.confmap_model.heads[0].output_stride,
            peak_threshold=0.2,
        )

        pipeline += KeyFilter(
            keep_keys=[
                "bbox",
                "center_instance_ind",
                "centroid",
                "centroid_confidence",
                "scale",
                "video_ind",
                "frame_ind",
                "center_instance_ind",
                "predicted_center_instance_points",
                "predicted_center_instance_confidences",
            ]
        )

        pipeline += PredictedCenterInstanceNormalizer(
            centroid_key="centroid",
            centroid_confidence_key="centroid_confidence",
            peaks_key="predicted_center_instance_points",
            peak_confidences_key="predicted_center_instance_confidences",
            new_centroid_key="predicted_centroid",
            new_centroid_confidence_key="predicted_centroid_confidence",
            new_peaks_key="predicted_instance",
            new_peak_confidences_key="predicted_instance_confidences",
        )

        self.pipeline = pipeline

        return pipeline

    def make_labeled_frames(
        self, examples: List[Dict[Text, tf.Tensor]], videos: List[sleap.Video]
    ) -> List[sleap.LabeledFrame]:
        # Pull out skeleton from the config.
        skeleton = self.confmap_config.data.labels.skeletons[0]

        # Group the examples by video and frame.
        grouped_examples = group_examples(examples)

        # Loop through grouped examples.
        predicted_frames = []
        for (video_ind, frame_ind), frame_examples in grouped_examples.items():

            # Create predicted instances from examples in the current frame.
            predicted_instances = []
            for example in frame_examples:
                predicted_instances.append(
                    sleap.PredictedInstance.from_arrays(
                        points=example["predicted_instance"],
                        point_confidences=example["predicted_instance_confidences"],
                        instance_score=example["predicted_centroid_confidence"],
                        skeleton=skeleton,
                    )
                )

            if len(predicted_instances) > 0:
                # Create labeled frame from predicted instances.
                predicted_frames.append(
                    sleap.LabeledFrame(
                        video=videos[video_ind],
                        frame_idx=frame_ind,
                        instances=predicted_instances,
                    )
                )

        return predicted_frames

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            self.make_pipeline()

        self.pipeline.providers = [data_provider]

        for example in self.pipeline.make_dataset():
            yield example

    def predict(self, data_provider: Provider, make_instances: bool = True):
        generator = self.predict_generator(data_provider)
        examples = list(generator)

        if make_instances:
            return self.make_labeled_frames(examples, videos=data_provider.videos)

        return examples


@attr.s(auto_attribs=True)
class BottomupPredictor:
    bottomup_config: TrainingJobConfig
    bottomup_model: Model
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(cls, bottomup_model_path: Text) -> "BottomupPredictor":
        """Create predictor from saved models."""
        # Load bottomup model.
        bottomup_config = TrainingJobConfig.load_json(bottomup_model_path)
        bottomup_keras_model_path = os.path.join(bottomup_model_path, "best_model.h5")
        bottomup_model = Model.from_config(bottomup_config.model)
        bottomup_model.keras_model = tf.keras.models.load_model(
            bottomup_keras_model_path, compile=False
        )

        return cls(bottomup_config=bottomup_config, bottomup_model=bottomup_model,)

    def make_pipeline(self, data_provider: Optional[Provider] = None) -> Pipeline:
        pipeline = Pipeline()
        if data_provider is not None:
            pipeline.providers = [data_provider]

        pipeline += Normalizer.from_config(self.bottomup_config.data.preprocessing)
        pipeline += Resizer.from_config(
            self.bottomup_config.data.preprocessing,
            keep_full_image=False,
            points_key=None,
        )

        pipeline += Prefetcher()

        pipeline += KerasModelPredictor(
            keras_model=self.bottomup_model.keras_model,
            model_input_keys="image",
            model_output_keys=[
                "predicted_confidence_maps",
                "predicted_part_affinity_fields",
            ],
        )
        pipeline += LocalPeakFinder(
            confmaps_stride=self.bottomup_model.heads[0].output_stride,
            peak_threshold=0.2,
            confmaps_key="predicted_confidence_maps",
            peaks_key="predicted_peaks",
            peak_vals_key="predicted_peak_confidences",
            peak_sample_inds_key="predicted_peak_sample_inds",
            peak_channel_inds_key="predicted_peak_channel_inds",
            keep_confmaps=False,
        )

        pipeline += PartAffinityFieldInstanceGrouper.from_config(
            self.bottomup_config.model.heads.multi_instance,
            max_edge_length=128,
            min_edge_score=0.05,
            n_points=10,
            min_instance_peaks=0,
            peaks_key="predicted_peaks",
            peak_scores_key="predicted_peak_confidences",
            channel_inds_key="predicted_peak_channel_inds",
            pafs_key="predicted_part_affinity_fields",
            predicted_instances_key="predicted_instances",
            predicted_peak_scores_key="predicted_peak_scores",
            predicted_instance_scores_key="predicted_instance_scores",
            keep_pafs=False,
        )

        pipeline += KeyFilter(
            keep_keys=[
                "scale",
                "video_ind",
                "frame_ind",
                "predicted_instances",
                "predicted_peak_scores",
                "predicted_instance_scores",
            ]
        )

        pipeline += PointsRescaler(
            points_key="predicted_instances", scale_key="scale", invert=True
        )

        self.pipeline = pipeline

        return pipeline

    def make_labeled_frames(
        self, examples: List[Dict[Text, tf.Tensor]], videos: List[sleap.Video]
    ) -> List[sleap.LabeledFrame]:
        # Pull out skeleton from the config.
        skeleton = self.bottomup_config.data.labels.skeletons[0]

        # Group the examples by video and frame.
        grouped_examples = group_examples(examples)

        # Loop through grouped examples.
        predicted_frames = []
        for (video_ind, frame_ind), frame_examples in grouped_examples.items():

            # Create predicted instances from examples in the current frame.
            predicted_instances = []
            for example in frame_examples:
                for points, confidences, instance_score in zip(
                    example["predicted_instances"],
                    example["predicted_peak_scores"],
                    example["predicted_instance_scores"],
                ):
                    predicted_instances.append(
                        sleap.PredictedInstance.from_arrays(
                            points=points,
                            point_confidences=confidences,
                            instance_score=instance_score,
                            skeleton=skeleton,
                        )
                    )

            if len(predicted_instances) > 0:
                # Create labeled frame from predicted instances.
                predicted_frames.append(
                    sleap.LabeledFrame(
                        video=videos[video_ind],
                        frame_idx=frame_ind,
                        instances=predicted_instances,
                    )
                )

        return predicted_frames

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            self.make_pipeline()

        self.pipeline.providers = [data_provider]

        for example in self.pipeline.make_dataset():
            yield example

    def predict(self, data_provider: Provider, make_instances: bool = True):
        generator = self.predict_generator(data_provider)
        examples = list(generator)

        if make_instances:
            return self.make_labeled_frames(examples, videos=data_provider.videos)

        return examples


@attr.s(auto_attribs=True)
class SingleInstancePredictor:
    confmap_config: TrainingJobConfig
    confmap_model: Model
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(cls, confmap_model_path: Text) -> "TopdownPredictor":
        """Create predictor from saved models."""

        # Load confmap model.
        confmap_config = TrainingJobConfig.load_json(confmap_model_path)
        confmap_keras_model_path = os.path.join(confmap_model_path, "best_model.h5")
        confmap_model = Model.from_config(confmap_config.model)
        confmap_model.keras_model = tf.keras.models.load_model(
            confmap_keras_model_path, compile=False
        )

        return cls(confmap_config=confmap_config, confmap_model=confmap_model,)

    def make_pipeline(self, data_provider: Optional[Provider] = None) -> Pipeline:

        pipeline = Pipeline()
        if data_provider is not None:
            pipeline.providers = [data_provider]

        pipeline += Normalizer.from_config(self.confmap_model.data.preprocessing)
        pipeline += Resizer.from_config(
            self.confmap_model.data.preprocessing,
            # keep_full_image=True,
            points_key=None,
        )

        pipeline += Prefetcher()

        pipeline += KerasModelPredictor(
            keras_model=self.confmap_model.keras_model,
            model_input_keys="image",
            model_output_keys="predicted_instance_confidence_maps",
        )
        pipeline += GlobalPeakFinder(
            confmaps_key="predicted_instance_confidence_maps",
            peaks_key="predicted_instance",
            peak_vals_key="predicted_instance_confidences",
            confmaps_stride=self.confmap_model.heads[0].output_stride,
            peak_threshold=0.2,
        )

        pipeline += KeyFilter(
            keep_keys=[
                "bbox",
                "scale",
                "video_ind",
                "frame_ind",
                "predicted_instance_points",
                "predicted_instance_confidence_maps",
            ]
        )

        self.pipeline = pipeline

        return pipeline

    def make_labeled_frames(
        self, examples: List[Dict[Text, tf.Tensor]], videos: List[sleap.Video]
    ) -> List[sleap.LabeledFrame]:
        # Pull out skeleton from the config.
        skeleton = self.confmap_config.data.labels.skeletons[0]

        # Group the examples by video and frame.
        grouped_examples = group_examples(examples)

        # Loop through grouped examples.
        predicted_frames = []
        for (video_ind, frame_ind), frame_examples in grouped_examples.items():

            # Create predicted instances from examples in the current frame.
            predicted_instances = []
            for example in frame_examples:
                predicted_instances.append(
                    sleap.PredictedInstance.from_arrays(
                        points=example["predicted_instance"],
                        point_confidences=example["predicted_instance_confidences"],
                        skeleton=skeleton,
                    )
                )

            if len(predicted_instances) > 0:
                # Create labeled frame from predicted instances.
                predicted_frames.append(
                    sleap.LabeledFrame(
                        video=videos[video_ind],
                        frame_idx=frame_ind,
                        instances=predicted_instances,
                    )
                )

        return predicted_frames

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            self.make_pipeline()

        self.pipeline.providers = [data_provider]

        for example in self.pipeline.make_dataset():
            yield example

    def predict(self, data_provider: Provider, make_instances: bool = True):
        generator = self.predict_generator(data_provider)
        examples = list(generator)

        if make_instances:
            return self.make_labeled_frames(examples, videos=data_provider.videos)

        return examples


def make_cli_parser():
    import argparse
    from sleap.util import frame_list

    parser = argparse.ArgumentParser()

    # Add args for entire pipeline
    parser.add_argument("data_path", help="Path to video file")
    parser.add_argument(
        "-m",
        "--model",
        dest="models",
        action="append",
        help="Path to trained model directory (with training_config.json). "
        "Multiple models can be specified, each preceded by --model.",
    )

    parser.add_argument(
        "--frames",
        type=frame_list,
        default="",
        help="List of frames to predict. Either comma separated list (e.g. 1,2,3) or "
        "a range separated by hyphen (e.g. 1-3, for 1,2,3). (default is entire video)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="The output filename to use for the predicted data.",
    )

    # TODO: better video parameters

    parser.add_argument(
        "--video.dataset", type=str, default="", help="The dataset for HDF5 videos."
    )

    parser.add_argument(
        "--video.input_format",
        type=str,
        default="",
        help="The input_format for HDF5 videos.",
    )

    # Add args for tracking
    Tracker.add_cli_parser_args(parser, arg_scope="tracking")

    return parser


def make_video_reader_from_cli(args):
    # TODO: better support for video params
    video_kwargs = dict(
        dataset=vars(args).get("video.dataset"),
        input_format=vars(args).get("video.input_format"),
    )

    video_reader = VideoReader.from_filepath(
        filename=args.data_path, example_indices=args.frames, **video_kwargs
    )

    return video_reader


def make_predictor_from_cli(args):
    # trained_model_configs = dict()
    trained_model_paths = dict()

    head_names = (
        "single_instance",
        "centroid",
        "centered_instance",
        "multi_instance",
    )

    for model_path in args.models:
        # Load the model config
        cfg = TrainingJobConfig.load_json(model_path)

        # Get the head from the model (i.e., what the model will predict)
        key = cfg.model.heads.which_oneof_attrib_name()

        # trained_model_configs[key] = cfg
        trained_model_paths[key] = model_path

    if "multi_instance" in trained_model_paths:
        predictor = BottomupPredictor.from_trained_models(
            trained_model_paths["multi_instance"]
        )
    elif "single_instance" in trained_model_paths:
        predictor = SingleInstancePredictor.from_trained_models(
            confmap_model_path=trained_model_paths["single_instance"]
        )
    elif (
        "centroid" in trained_model_paths and "centered_instance" in trained_model_paths
    ):
        predictor = TopdownPredictor.from_trained_models(
            centroid_model_path=trained_model_paths["centroid"],
            confmap_model_path=trained_model_paths["centered_instance"],
        )
    else:
        # TODO: support for tracking on previous predictions w/o model
        raise ValueError(
            f"Unable to run inference with {list(trained_model_paths.keys())} heads."
        )

    return predictor


def make_tracker_from_cli(args):
    policy_args = util.make_scoped_dictionary(vars(args), exclude_nones=True)

    tracker_name = "None"
    if "tracking" in policy_args:
        tracker_name = policy_args["tracking"].get("tracker", "None")

    if tracker_name.lower() != "none":
        tracker = Tracker.make_tracker_by_name(**policy_args["tracking"])
        return tracker

    return None


def save_predictions_from_cli(args, predicted_frames):
    from sleap import Labels

    if args.output:
        output_path = args.output
    else:
        out_dir = os.path.dirname(args.data_path)
        out_name = os.path.basename(args.data_path) + ".predictions.h5"
        output_path = os.path.join(out_dir, out_name)

    labels = Labels(labeled_frames=predicted_frames)

    print(f"Saving: {output_path}")
    Labels.save_file(labels, output_path)


def main():
    """CLI for running inference."""

    parser = make_cli_parser()
    args, _ = parser.parse_known_args()
    print(args)

    video_reader = make_video_reader_from_cli(args)
    print("Frames:", len(video_reader))

    predictor = make_predictor_from_cli(args)

    # Run inference!
    predicted_frames = predictor.predict(video_reader)

    save_predictions_from_cli(args, predicted_frames)


if __name__ == "__main__":
    main()
