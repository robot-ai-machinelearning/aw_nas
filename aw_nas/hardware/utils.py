# -*- coding: utf-8 -*-
import copy
import inspect
import json
import os
import pickle
from collections import namedtuple
from inspect import signature

import numpy as np
import yaml
from scipy import stats
from sklearn import linear_model
from sklearn.metrics import mean_squared_error, r2_score

from aw_nas.hardware.base import (BaseHardwareObjectiveModel,
                                  BasePerformanceModel,
                                  MixinProfilingSearchSpace, Preprocessor)
from aw_nas.ops import get_op

Prim_ = namedtuple(
    "Prim",
    ["prim_type", "spatial_size", "C", "C_out", "stride", "affine", "kwargs"],
)


class Prim(Prim_):
    def __new__(cls, prim_type, spatial_size, C, C_out, stride, affine, **kwargs):
        position_params = ["C", "C_out", "stride", "affine"]
        prim_constructor = get_op(prim_type)
        prim_sig = signature(prim_constructor)
        params = prim_sig.parameters
        for name, param in params.items():
            if param.default != inspect._empty:
                if name in position_params:
                    continue
                if kwargs.get(name) is None:
                    kwargs[name] = param.default
            else:
                assert name in position_params or name in kwargs, \
                    "{} is a non-default parameter which should be provided explicitly.".format(
                    name)

        assert set(params.keys()) == set(
            position_params + list(kwargs.keys())),\
            ("The passed parameters are different from the formal parameter list of primitive "
             "type `{}`, expected {}, got {}").format(
                 prim_type,
                 str(params.keys()),
                 str(position_params + list(kwargs.keys()))
             )

        kwargs = tuple(
            sorted([(k, v) for k, v in kwargs.items() if v is not None]))
        return super(Prim, cls).__new__(
            cls,
            prim_type,
            int(spatial_size),
            int(C),
            int(C_out),
            int(stride),
            affine,
            kwargs,
        )

    def _asdict(self):
        origin_dict = dict(super(Prim, self)._asdict())
        kwargs = origin_dict.pop("kwargs")
        origin_dict.update(dict(kwargs))
        return origin_dict


def assemble_profiling_nets_from_file(fname,
                                      base_cfg_fname,
                                      image_size=224,
                                      sample=None,
                                      max_layers=20):
    with open(fname, "r") as f:
        prof_prims = yaml.load(f)
    with open(base_cfg_fname, "r") as f:
        base_cfg = yaml.load(f)
    return assemble_profiling_nets(prof_prims, base_cfg, image_size, sample,
                                   max_layers)


def assemble_profiling_nets(profiling_primitives,
                            base_cfg_template,
                            image_size=224,
                            sample=None,
                            max_layers=20):
    """
    Args:
        profiling_primitives: (list of dict)
            possible keys: prim_type, spatial_size, C, C_out, stride, primitive_kwargs
            (Don't use dict and list that is unhashable. Use tuple instead: (key, value) )
        base_cfg_template: (dict) final configuration template
        image_size: (int) the inputs size
        sample: (int) the number of nets
        max_layers: (int) the number of max layers of each net (glue layers do not count)

    Returns:
        a generator of yaml configs

    This function assembles all profiling primitives into multiple networks, which takes a several steps:
    1. Each network has a stride=2 layer as the first conv layer (like many convolution network, in order to reduce the size of feature map.)
    2. Find a available primitive for current spatial_size and current channel number:
        a). If there is a primitive has exactly same channel number and spatial size with previous primitive, append it to genotype;
        b). else we select a primitive which has the same or smaller spatial size, and insert a glue layer between them to make the number of channels consistant.
    3. Iterate profiling primitives until there is not available primitive or the number of genotype's layers exceeds the max layer.
    """

    if sample is None:
        sample = np.inf

    # genotypes: "[prim_type, *params], [] ..."
    profiling_primitives = sorted(profiling_primitives,
                                  key=lambda x: (x["C"], x["stride"]))
    ith_arch = 0
    glue_layer = lambda spatial_size, C, C_out, stride=1: {
        "prim_type": "conv_1x1",
        "spatial_size": spatial_size,
        "C": C,
        "C_out": C_out,
        "stride": stride,
        "affine": True,
    }

    # use C as the index of df to accelerate query
    available_idx = list(range(len(profiling_primitives)))
    channel_to_idx = {}
    for i, prim in enumerate(profiling_primitives):
        channel_to_idx[prim["C"]] = channel_to_idx.get(prim["C"], []) + [i]
    channel_to_idx = {k: set(v) for k, v in channel_to_idx.items()}

    while len(available_idx) > 0 and ith_arch < sample:
        ith_arch += 1
        geno = []

        # the first conv layer reduing the size of feature map.
        sampled_prim = profiling_primitives[available_idx[0]]
        cur_channel = int(sampled_prim["C"])
        first_cov_op = {
            "prim_type": "conv_3x3",
            "spatial_size": image_size,
            "C": 3,
            "C_out": cur_channel,
            "stride": 2,
            "affine": True,
        }
        cur_size = round(image_size / 2)
        geno.append(first_cov_op)

        for _ in range(max_layers):
            if len(available_idx) == 0:
                break

            try:
                # find layer which has exactly same channel number and spatial size with the previous one
                idx = channel_to_idx[cur_channel]
                if len(idx) == 0:
                    raise ValueError
                for i in idx:
                    sampled_prim = profiling_primitives[i]
                    if sampled_prim["spatila_size"] == cur_size:
                        break
                else:
                    raise ValueError
            except:
                # or find a layer which has arbitrary channel number but has smaller spatial size
                # we need to assure that spatial size decreases as the layer number (or upsample layer will be needed.)
                for i in available_idx:
                    if profiling_primitives[i]["spatial_size"] <= cur_size:
                        sampled_prim = profiling_primitives[i]
                        break
                else:
                    break

                out_channel = int(sampled_prim["C"])
                spatial_size = int(sampled_prim["spatial_size"])
                stride = int(round(cur_size / spatial_size))
                assert isinstance(
                    stride, int) and stride > 0, "stride: {stride}".format(
                        stride=stride)
                geno.append(
                    glue_layer(cur_size, cur_channel, out_channel, stride))

            cur_channel = int(sampled_prim["C_out"])
            cur_size = int(
                round(sampled_prim["spatial_size"] / sampled_prim["stride"]))

            available_idx.remove(i)
            channel_to_idx[sampled_prim["C"]].remove(i)

            geno.append(sampled_prim)

        base_cfg_template["final_model_cfg"]["genotypes"] = geno
        yield copy.deepcopy(base_cfg_template)


class BlockSumPreprocessor(Preprocessor):
    NAME = "block_sum"

    def __init__(self, preprocessors=None, schedule_cfg=None):
        super().__init__(preprocessors, schedule_cfg)

    def __call__(self, unpreprocessed, **kwargs):
        for prof_net in unpreprocessed:
            for ith_prof in prof_net:
                block_sum = {}
                for prim in ith_prof["primitives"]:
                    for k, perf in prim["performances"].items():
                        block_sum[k] = block_sum.get(k, 0.) + perf
                for k, perf in block_sum.items():
                    ith_prof["block_sum_{}".format(k)] = block_sum[k]
            yield prof_net


class FlattenPreprocessor(Preprocessor):
    NAME = "flatten"

    def __init__(self, preprocessors=None, schedule_cfg=None):
        super().__init__(preprocessors, schedule_cfg)

    def __call__(self, unpreprocessed, **kwargs):
        for prof_net in unpreprocessed:
            for ith_prof in prof_net:
                yield ith_prof


class RemoveAnomalyPreprocessor(Preprocessor):
    NAME = "remove_anomaly"

    def __init__(self, preprocessors=None, schedule_cfg=None):
        super().__init__(preprocessors, schedule_cfg)

    def __call__(self, unpreprocessed, **kwargs):
        is_training = kwargs.get("is_training", True)
        if not is_training:
            for net in unpreprocessed:
                yield net
        tolerance_std = kwargs.get("tolerance_std", 0.1)
        for prof_net in unpreprocessed:
            # FIXME: assert every primitive has the performance keys.
            perf_keys = prof_net[0]["primitives"][0]["performances"].keys()
            block_sum_avg = {
                k: np.mean([
                    ith_prof["block_sum_{}".format(k)] for ith_prof in prof_net
                ])
                for k in perf_keys
            }
            filtered_net = []
            for ith_prof in prof_net:
                for k in perf_keys:
                    if abs(ith_prof["block_sum_{}".format(k)] -
                           block_sum_avg[k]
                           ) > block_sum_avg[k] * tolerance_std:
                        break
                else:
                    filtered_net += [ith_prof]
            yield filtered_net


class ExtractSumFeaturesPreprocessor(Preprocessor):
    NAME = "extract_sum_features"

    def __init__(self, preprocessors=None, schedule_cfg=None):
        super().__init__(preprocessors, schedule_cfg)

    def __call__(self, unpreprocessed, **kwargs):
        is_training = kwargs.get("is_training", True)
        performance = kwargs.get("performance", "latency")
        unpreprocessed = list(unpreprocessed)
        train_x = []
        train_y = []
        for prof_net in unpreprocessed:
            train_x += [[prof_net["block_sum_{}".format(performance)]]]
            if is_training:
                train_y += [prof_net["overall_{}".format(performance)]]
        train_x = np.array(train_x).reshape(-1, 1)
        if is_training:
            train_y = np.array(train_y).reshape(-1)
            return unpreprocessed, train_x, train_y
        return unpreprocessed, train_x


class TableBasedModel(BaseHardwareObjectiveModel):
    NAME = "table"

    def __init__(
        self,
        mixin_search_space,
        prof_prims_cfg={},
        preprocessors=("remove_anomaly", "flatten"),
        performance="latency",
        schedule_cfg=None,
    ):
        super(TableBasedModel, self).__init__(schedule_cfg)
        self.prof_prims_cfg = prof_prims_cfg
        self.performance = performance
        self.mixin_search_space = mixin_search_space

        self.preprocessor = Preprocessor(preprocessors)

        self._table = {}

    def train(self, prof_nets):
        """
        Args:
            prof_nets: a list of dict, [{"primitives": [], "overall_performances": {}}, ...]
            performance: a list of value of each net's performance
        """
        for net in prof_nets:
            for prim in net.get("primitives", []):
                perf = prim.pop("performances")[self.performance]
                prim = Prim(**prim)
                self._table[prim] = self._table.get(prim, []) + [perf]
        
        self._table = {k: np.mean(v) for k, v in self._table.items()}
        return self

    def predict(self, rollout, assemble_fn=sum):
        primitives = self.mixin_search_space.rollout_to_primitives(
            rollout, **self.prof_prims_cfg)
        perfs = []
        for prim in primitives:
            perf = self._table.get(prim)
            if perf is None:
                self.logger.warn(
                    "primitive %s is not found in the table, return default value 0.",
                    prim)
                perf = 0.
            perfs += [perf]
        return assemble_fn(perfs)

    def save(self, path):
        pickled_table = [(k._asdict(), v) for k, v in self._table.items()]
        with open(path, "wb") as fw:
            pickle.dump(
                {
                    "table": pickled_table,
                }, fw)

    def load(self, path):
        with open(path, "rb") as fr:
            m = pickle.load(fr)
        self._table = {Prim(**k): v for k, v in m["table"]}

class RegressionHardwareObjectiveModel(TableBasedModel):
    NAME = "regression"

    def __init__(
        self,
        mixin_search_space,
        prof_prims_cfg={},
        preprocessors=("block_sum", "remove_anomaly", "flatten",
                       "extract_sum_features"),
        performance="latency",
        schedule_cfg=None,
    ):
        super().__init__(mixin_search_space, prof_prims_cfg, preprocessors,
                         performance, schedule_cfg)
        self.regression_model = linear_model.LinearRegression()

        assert isinstance(mixin_search_space, MixinProfilingSearchSpace)

    def train(self, prof_nets):
        prof_nets, train_x, train_y = self.preprocessor(
            prof_nets, is_training=True, performance=self.performance)
        super().train(prof_nets)
        self.regression_model.fit(train_x, train_y)
        return self

    def predict(self, rollout):
        primitives = self.mixin_search_space.rollout_to_primitives(
            rollout, **self.prof_prims_cfg)
        perfs = super().predict(rollout, assemble_fn=lambda x: x)
        primitives = [p._asdict() for p in primitives]
        for prim, perf in zip(primitives, perfs):
            prim["performances"] = {self.performance: perf}
        prof_nets = [[{"primitives": primitives}]]
        prof_nets, test_x = self.preprocessor(
            prof_nets, is_training=False, performance=self.performance)
        return float(self.regression_model.predict(test_x)[0])

    def save(self, path):
        pickled_table = [(k._asdict(), v) for k, v in self._table.items()]
        with open(path, "wb") as fw:
            pickle.dump(
                {
                    "table": pickled_table,
                    "model": self.regression_model
                }, fw)

    def load(self, path):
        with open(path, "rb") as fr:
            m = pickle.load(fr)
        self._table = {Prim(**k): v for k, v in m["table"]}
        self.regression_model = m["model"]


def iterate(prof_prim_dir):
    for _dir in os.listdir(prof_prim_dir):
        cur_dir = os.path.join(prof_prim_dir, _dir)
        if not os.path.isdir(cur_dir):
            continue
        prof_net = []
        for f in os.listdir(cur_dir):
            if not f.endswith("yaml"):
                continue
            with open(os.path.join(cur_dir, f), "r") as fr:
                prof_net += [yaml.load(fr)]
        yield prof_net