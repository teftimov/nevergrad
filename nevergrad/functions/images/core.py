# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from pathlib import Path

import numpy as np
import PIL.Image
import torch.nn as nn
import torch
import torchvision
from nevergrad.functions.images.imageLosses import SumAbsoluteDifferencesLoss, Koncept512Loss
from torchvision.models import resnet50
import torchvision.transforms as tr

import nevergrad as ng
import nevergrad.common.typing as tp
from .. import base
# pylint: disable=abstract-method


class Image(base.ExperimentFunction):
    def __init__(self, problem_name: str = "recovering", index: int = 0, loss=SumAbsoluteDifferencesLoss) -> None:
        """
        problem_name: the type of problem we are working on.
           recovering: we directly try to recover the target image.
        index: the index of the problem, inside the problem type.
           For example, if problem_name is "recovering" and index == 0,
           we try to recover the face of O. Teytaud.
        """

        # Storing high level information.
        self.domain_shape = (256, 256, 3)
        self.problem_name = problem_name
        self.index = index

        # Storing data necessary for the problem at hand.
        assert problem_name == "recovering"  # For the moment we have only this one.
        assert index == 0  # For the moment only 1 target.
        # path = os.path.dirname(__file__) + "/headrgb_olivier.png"
        path = Path(__file__).with_name("headrgb_olivier.png")
        image = PIL.Image.open(path).resize((self.domain_shape[0], self.domain_shape[1]), PIL.Image.ANTIALIAS)
        self.data = np.asarray(image)[:, :, :3]  # 4th Channel is pointless here, only 255.
        # parametrization
        array = ng.p.Array(init=128 * np.ones(self.domain_shape), mutable_sigma=True)
        array.set_mutation(sigma=35)
        array.set_bounds(lower=0, upper=255.99, method="clipping", full_range_sampling=True)
        max_size = ng.p.Scalar(lower=1, upper=200).set_integer_casting()
        array.set_recombination(ng.p.mutation.Crossover(axis=(0, 1), max_size=max_size)).set_name("")  # type: ignore

        super().__init__(self._loss, array)
        self.register_initialization(problem_name=problem_name, index=index, loss=loss.__name__)
        self._descriptors.update(problem_name=problem_name, index=index, loss=loss.__name__)
        self.loss_function = loss(reference=self.data)

    def _loss(self, x: np.ndarray) -> float:
        return self.loss_function(x)


# #### Adversarial attacks ##### #


class Normalize(nn.Module):

    def __init__(self, mean: tp.ArrayLike, std: tp.ArrayLike) -> None:
        super().__init__()
        self.mean = torch.Tensor(mean)
        self.std = torch.Tensor(std)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return (x - self.mean.type_as(x)[None, :, None, None]) / self.std.type_as(x)[None, :, None, None]


class Resnet50(nn.Module):

    def __init__(self) -> None:
        super().__init__()
        self.norm = Normalize(mean=[0.485, 0.456, 0.406],
                              std=[0.229, 0.224, 0.225])
        self.model = resnet50(pretrained=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(self.norm(x))


class TestClassifier(nn.Module):

    def __init__(self, image_size: int = 224) -> None:
        super().__init__()
        self.model = nn.Linear(image_size * image_size * 3, 10)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(x.view(x.shape[0], -1))


# pylint: disable=too-many-arguments,too-many-instance-attributes
class ImageAdversarial(base.ExperimentFunction):

    def __init__(self, classifier: nn.Module, image: torch.Tensor, label: int = 0, targeted: bool = False,
                 epsilon: float = 0.05) -> None:
        # TODO add crossover params in args + criterion
        """
        params : needs to be detailed
        """
        self.targeted = targeted
        self.epsilon = epsilon
        self.image = image  # if (image is not None) else torch.rand((3, 224, 224))
        self.label = torch.Tensor([label])  # if (label is not None) else torch.Tensor([0])
        self.label = self.label.long()
        self.classifier = classifier  # if (classifier is not None) else Classifier()
        self.criterion = nn.CrossEntropyLoss()
        self.imsize = self.image.shape[1]

        array = ng.p.Array(init=np.zeros(self.image.shape), mutable_sigma=True, ).set_name("")
        array.set_mutation(sigma=self.epsilon / 10)
        array.set_bounds(lower=-self.epsilon, upper=self.epsilon, method="clipping", full_range_sampling=True)
        max_size = ng.p.Scalar(lower=1, upper=200).set_integer_casting()
        array.set_recombination(ng.p.mutation.Crossover(axis=(1, 2), max_size=max_size))  # type: ignore

        super().__init__(self._loss, array)
        self.register_initialization(classifier=classifier, image=image, label=label,
                                     targeted=targeted, epsilon=epsilon)
        # classifier and image cant be set as descriptors
        self.add_descriptors(label=label, targeted=targeted, epsilon=epsilon)

    def _loss(self, x: np.ndarray) -> float:
        output_adv = self._get_classifier_output(x)
        value = float(self.criterion(output_adv, self.label).item())
        return value * (1.0 if self.targeted else -1.0)

    def _get_classifier_output(self, x: np.ndarray) -> tp.Any:
        # call to the classifier given the input array
        y = torch.Tensor(x)
        image_adv = torch.clamp(self.image + y, 0, 1)
        image_adv = image_adv.view(1, 3, self.imsize, self.imsize)
        return self.classifier(image_adv)

    # pylint: disable=arguments-differ
    def evaluation_function(self, x: np.ndarray) -> float:  # type: ignore
        """Returns wether the attack worked or not
        """
        output_adv = self._get_classifier_output(x)
        _, pred = torch.max(output_adv, axis=1)
        actual = int(self.label)
        return float(pred == actual if self.targeted else pred != actual)

    @classmethod
    def make_folder_functions(
            cls,
            folder: tp.Optional[tp.PathLike],
            model: str = "resnet50",
    ) -> tp.Generator["ImageAdversarial", None, None]:
        """

        Parameters
        ----------
        folder: str or None
            folder to use for reference images. If None, 1 random image is created.
        model: str
            model name to use

        Yields
        ------
        ExperimentFunction
            an experiment function corresponding to 1 of the image of the provided folder dataset.
        """
        assert model in {"resnet50", "test"}
        tags = {"folder": "#FAKE#" if folder is None else Path(folder).name, "model": model}
        classifier: tp.Any = Resnet50() if model == "resnet50" else TestClassifier()
        imsize = 224
        transform = tr.Compose([tr.Resize(imsize), tr.CenterCrop(imsize), tr.ToTensor()])
        if folder is None:
            x = torch.zeros(1, 3, 224, 224)
            _, pred = torch.max(classifier(x), axis=1)
            data_loader: tp.Iterable[tp.Tuple[tp.Any, tp.Any]] = [(x, pred)]
        elif Path(folder).is_dir():
            ifolder = torchvision.datasets.ImageFolder(folder, transform)
            data_loader = torch.utils.DataLoader(ifolder, batch_size=1, shuffle=True,
                                                 num_workers=8, pin_memory=True)
        else:
            raise ValueError(f"{folder} is not a valid folder.")
        for data, target in data_loader:
            _, pred = torch.max(classifier(data), axis=1)
            if pred == target:
                func = cls._with_tag(tags=tags, classifier=classifier, image=data[0],
                                     label=int(target), targeted=False, epsilon=0.05)
                yield func

    @classmethod
    def _with_tag(
            cls,
            tags: tp.Dict[str, str],
            **kwargs: tp.Any,
    ) -> "ImageAdversarial":
        # generates an instance with a hack so that additional tags are propagated to copies
        func = cls(**kwargs)
        func.add_descriptors(**tags)
        func._initialization_func = cls._with_tag  # type: ignore
        assert func._initialization_kwargs is not None
        func._initialization_kwargs["tags"] = tags
        return func


class ImageFromPGAN(base.ExperimentFunction):
    """
    Creates face images using a GAN from pytorch GAN zoo trained on celebAHQ and optimizes the noise vector of the GAN

    problem_name: the type of problem we are working on.
    initial_noise: the initial noise of the GAN. It should be of dimension (1, 512). If None, it is defined randomly.
    use_gpu: whether to use gpus to compute the images
    scorer: which scorer to use for the images
    mutable_sigma: whether the sigma should be mutable
    n_mutations: number of mutations
    """

    def __init__(self, initial_noise: np.ndarray = None, use_gpu: bool = True, scorer=Koncept512Loss, mutable_sigma=True, n_mutations=35) -> None:
        if torch.cuda.is_available():
            use_gpu = False

        # Storing high level information..
        self.pgan_model = torch.hub.load('facebookresearch/pytorch_GAN_zoo:hub',
                               'PGAN', model_name='celebAHQ-512',
                               pretrained=True, useGPU=use_gpu)

        self.noise_shape = (1, 512)
        if initial_noise is None:
            initial_noise = np.random.normal(size=self.noise_shape)
        assert initial_noise.shape == self.noise_shape, f'The shape of the initial noise vector was {initial_noise.shape}, it should be {self.noise_shape}'

        array = ng.p.Array(init=initial_noise, mutable_sigma=mutable_sigma)
        # parametrization
        array.set_mutation(sigma=n_mutations)
        array.set_recombination(ng.p.mutation.Crossover(axis=(0, 1))).set_name("")

        super().__init__(self._loss, array)
        self.loss_function = scorer()
        self.register_initialization(initial_noise=initial_noise, use_gpu=use_gpu, scorer=scorer.__name__, mutable_sigma=mutable_sigma, n_mutations=n_mutations)
        self._descriptors.update(initial_noise=initial_noise, use_gpu=use_gpu, scorer=scorer.__name__, mutable_sigma=mutable_sigma, n_mutations=n_mutations)

    def _loss(self, x: np.ndarray) -> float:
        image = self._generate_images(x)
        loss = self.loss_function(image)
        return loss

    def _generate_images(self, x: np.ndarray):
        noise = torch.tensor(x.astype('float32'))
        return self.pgan_model.test(noise).permute(0, 2, 3, 1).cpu().numpy()