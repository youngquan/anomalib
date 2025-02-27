"""EfficientAd: Accurate Visual Anomaly Detection at Millisecond-Level Latencies.

https://arxiv.org/pdf/2303.14535.pdf.
"""

# Copyright (C) 2023-2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import logging
from pathlib import Path
from typing import Any

import albumentations as A  # noqa: N812
import numpy as np
import torch
import tqdm
from albumentations.pytorch import ToTensorV2
from lightning.pytorch.utilities.types import STEP_OUTPUT
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder

from anomalib import LearningType
from anomalib.data.utils import DownloadInfo, download_and_extract
from anomalib.models.components import AnomalyModule

from .torch_model import EfficientAdModel, EfficientAdModelSize, reduce_tensor_elems

logger = logging.getLogger(__name__)

IMAGENETTE_DOWNLOAD_INFO = DownloadInfo(
    name="imagenette2.tgz",
    url="https://s3.amazonaws.com/fast-ai-imageclas/imagenette2.tgz",
    checksum="fe2fc210e6bb7c5664d602c3cd71e612",
)

WEIGHTS_DOWNLOAD_INFO = DownloadInfo(
    name="efficientad_pretrained_weights.zip",
    url="https://github.com/openvinotoolkit/anomalib/releases/download/efficientad_pretrained_weights/efficientad_pretrained_weights.zip",
    checksum="ec6113d728969cd233271eeed7d692f2",
)


class TransformsWrapper:
    """Transforms wrapper.

    Args:
        t (A.Compose): Albumentations transforms.
    """

    def __init__(self, t: A.Compose) -> None:
        self.transforms = t

    def __call__(self, img: np.ndarray, *args, **kwargs) -> dict[str, Any]:
        """Apply the transforms to the given image.

        Args:
            img (np.ndarray): Input image.
            args: Additional arguments.
            kwargs: Additional keyword arguments.

        Returns:
            dict[str, Any]: Output image.
        """
        del args, kwargs  # Unused arguments.

        return self.transforms(image=np.array(img))


class EfficientAd(AnomalyModule):
    """PL Lightning Module for the EfficientAd algorithm.

    Args:
        input_size (tuple): size of input images
            Defaults to ``(256, 256)``.
        teacher_out_channels (int): number of convolution output channels
            Defaults to ``384``.
        model_size (str): size of student and teacher model
            Defaults to ``EfficientAdModelSize.S``.
        lr (float): learning rate
            Defaults to ``0.0001``.
        weight_decay (float): optimizer weight decay
            Defaults to ``0.00001``.
        padding (bool): use padding in convoluional layers
            Defaults to ``False``.
        pad_maps (bool): relevant if padding is set to False. In this case, pad_maps = True pads the
            output anomaly maps so that their size matches the size in the padding = True case.
            Defaults to ``True``.
        batch_size (int): batch size for imagenet dataloader
            Defaults to ``1``.
    """

    def __init__(
        self,
        input_size: tuple[int, int] = (256, 256),
        teacher_out_channels: int = 384,
        model_size: EfficientAdModelSize = EfficientAdModelSize.S,
        lr: float = 0.0001,
        weight_decay: float = 0.00001,
        padding: bool = False,
        pad_maps: bool = True,
        batch_size: int = 1,
    ) -> None:
        super().__init__()

        self.model_size = model_size
        self.model: EfficientAdModel = EfficientAdModel(
            teacher_out_channels=teacher_out_channels,
            input_size=input_size,
            model_size=model_size,
            padding=padding,
            pad_maps=pad_maps,
        )
        self.batch_size = batch_size
        self.image_size = input_size
        self.lr = lr
        self.weight_decay = weight_decay

        self.prepare_pretrained_model()
        self.prepare_imagenette_data()

    def prepare_pretrained_model(self) -> None:
        """Prepare the pretrained teacher model."""
        pretrained_models_dir = Path("./pre_trained/")
        if not (pretrained_models_dir / "efficientad_pretrained_weights").is_dir():
            download_and_extract(pretrained_models_dir, WEIGHTS_DOWNLOAD_INFO)
        teacher_path = (
            pretrained_models_dir / "efficientad_pretrained_weights" / f"pretrained_teacher_{self.model_size}.pth"
        )
        logger.info(f"Load pretrained teacher model from {teacher_path}")
        self.model.teacher.load_state_dict(torch.load(teacher_path, map_location=torch.device(self.device)))

    def prepare_imagenette_data(self) -> None:
        """Prepare ImageNette dataset transformations."""
        self.data_transforms_imagenet = A.Compose(
            [  # We obtain an image P ∈ R 3x256x256 from ImageNet by choosing a random image,
                A.Resize(self.image_size[0] * 2, self.image_size[1] * 2),  # resizing it to 512 x 512,
                A.ToGray(p=0.3),  # converting it to gray scale with a probability of 0.3
                A.CenterCrop(self.image_size[0], self.image_size[1]),  # and cropping the center 256 x 256 pixels
                A.ToFloat(always_apply=False, p=1.0, max_value=255),
                ToTensorV2(),
            ],
        )

        imagenet_dir = Path("./datasets/imagenette")
        if not imagenet_dir.is_dir():
            download_and_extract(imagenet_dir, IMAGENETTE_DOWNLOAD_INFO)
        imagenet_dataset = ImageFolder(imagenet_dir, transform=TransformsWrapper(t=self.data_transforms_imagenet))
        self.imagenet_loader = DataLoader(imagenet_dataset, batch_size=self.batch_size, shuffle=True, pin_memory=True)
        self.imagenet_iterator = iter(self.imagenet_loader)

    @torch.no_grad()
    def teacher_channel_mean_std(self, dataloader: DataLoader) -> dict[str, torch.Tensor]:
        """Calculate the mean and std of the teacher models activations.

        Adapted from https://math.stackexchange.com/a/2148949

        Args:
            dataloader (DataLoader): Dataloader of the respective dataset.

        Returns:
            dict[str, torch.Tensor]: Dictionary of channel-wise mean and std
        """
        arrays_defined = False
        n: torch.Tensor | None = None
        chanel_sum: torch.Tensor | None = None
        chanel_sum_sqr: torch.Tensor | None = None

        for batch in tqdm.tqdm(dataloader, desc="Calculate teacher channel mean & std", position=0, leave=True):
            y = self.model.teacher(batch["image"].to(self.device))
            if not arrays_defined:
                _, num_channels, _, _ = y.shape
                n = torch.zeros((num_channels,), dtype=torch.int64, device=y.device)
                chanel_sum = torch.zeros((num_channels,), dtype=torch.float64, device=y.device)
                chanel_sum_sqr = torch.zeros((num_channels,), dtype=torch.float64, device=y.device)
                arrays_defined = True

            n += y[:, 0].numel()
            chanel_sum += torch.sum(y, dim=[0, 2, 3])
            chanel_sum_sqr += torch.sum(y**2, dim=[0, 2, 3])

        assert n is not None

        channel_mean = chanel_sum / n

        channel_std = (torch.sqrt((chanel_sum_sqr / n) - (channel_mean**2))).float()[None, :, None, None]
        channel_mean = channel_mean.float()[None, :, None, None]

        return {"mean": channel_mean, "std": channel_std}

    @torch.no_grad()
    def map_norm_quantiles(self, dataloader: DataLoader) -> dict[str, torch.Tensor]:
        """Calculate 90% and 99.5% quantiles of the student(st) and autoencoder(ae).

        Args:
            dataloader (DataLoader): Dataloader of the respective dataset.

        Returns:
            dict[str, torch.Tensor]: Dictionary of both the 90% and 99.5% quantiles
            of both the student and autoencoder feature maps.
        """
        maps_st = []
        maps_ae = []
        logger.info("Calculate Validation Dataset Quantiles")
        for batch in tqdm.tqdm(dataloader, desc="Calculate Validation Dataset Quantiles", position=0, leave=True):
            for img, label in zip(batch["image"], batch["label"], strict=True):
                if label == 0:  # only use good images of validation set!
                    output = self.model(img.to(self.device), normalize=False)
                    map_st = output["map_st"]
                    map_ae = output["map_ae"]
                    maps_st.append(map_st)
                    maps_ae.append(map_ae)

        qa_st, qb_st = self._get_quantiles_of_maps(maps_st)
        qa_ae, qb_ae = self._get_quantiles_of_maps(maps_ae)
        return {"qa_st": qa_st, "qa_ae": qa_ae, "qb_st": qb_st, "qb_ae": qb_ae}

    def _get_quantiles_of_maps(self, maps: list[torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
        """Calculate 90% and 99.5% quantiles of the given anomaly maps.

        If the total number of elements in the given maps is larger than 16777216
        the returned quantiles are computed on a random subset of the given
        elements.

        Args:
            maps (list[torch.Tensor]): List of anomaly maps.

        Returns:
            tuple[torch.Tensor, torch.Tensor]: Two scalars - the 90% and the 99.5% quantile.
        """
        maps_flat = reduce_tensor_elems(torch.cat(maps))
        qa = torch.quantile(maps_flat, q=0.9).to(self.device)
        qb = torch.quantile(maps_flat, q=0.995).to(self.device)
        return qa, qb

    def configure_optimizers(self) -> torch.optim.Optimizer:
        """Configure optimizers."""
        optimizer = torch.optim.Adam(
            list(self.model.student.parameters()) + list(self.model.ae.parameters()),
            lr=self.lr,
            weight_decay=self.weight_decay,
        )

        if self.trainer.max_epochs < 0 and self.trainer.max_steps < 0:
            msg = "A finite number of steps or epochs must be defined"
            raise ValueError(msg)

        # lightning stops training when either 'max_steps' or 'max_epochs' is reached (earliest),
        # so actual training steps need to be determined here
        if self.trainer.max_epochs < 0:
            # max_epochs not set
            num_steps = self.trainer.max_steps
        elif self.trainer.max_steps < 0:
            # max_steps not set -> determine steps as 'max_epochs' * 'steps in a single training epoch'
            num_steps = self.trainer.max_epochs * len(self.trainer.datamodule.train_dataloader())
        else:
            num_steps = min(
                self.trainer.max_steps,
                self.trainer.max_epochs * len(self.trainer.datamodule.train_dataloader()),
            )

        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=int(0.95 * num_steps), gamma=0.1)
        return {"optimizer": optimizer, "lr_scheduler": scheduler}

    def on_train_start(self) -> None:
        """Calculate or load the channel-wise mean and std of the training dataset and push to the model."""
        if not self.model.is_set(self.model.mean_std):
            channel_mean_std = self.teacher_channel_mean_std(self.trainer.datamodule.train_dataloader())
            self.model.mean_std.update(channel_mean_std)

    def training_step(self, batch: dict[str, str | torch.Tensor], *args, **kwargs) -> dict[str, torch.Tensor]:
        """Perform the training step for EfficientAd returns the student, autoencoder and combined loss.

        Args:
            batch (batch: dict[str, str | torch.Tensor]): Batch containing image filename, image, label and mask
            args: Additional arguments.
            kwargs: Additional keyword arguments.

        Returns:
          Loss.
        """
        del args, kwargs  # These variables are not used.

        try:
            # infinite dataloader; [0] getting the image not the label
            batch_imagenet = next(self.imagenet_iterator)[0]["image"].to(self.device)
        except StopIteration:
            self.imagenet_iterator = iter(self.imagenet_loader)
            batch_imagenet = next(self.imagenet_iterator)[0]["image"].to(self.device)

        loss_st, loss_ae, loss_stae = self.model(batch=batch["image"], batch_imagenet=batch_imagenet)

        loss = loss_st + loss_ae + loss_stae
        self.log("train_st", loss_st.item(), on_epoch=True, prog_bar=True, logger=True)
        self.log("train_ae", loss_ae.item(), on_epoch=True, prog_bar=True, logger=True)
        self.log("train_stae", loss_stae.item(), on_epoch=True, prog_bar=True, logger=True)
        self.log("train_loss", loss.item(), on_epoch=True, prog_bar=True, logger=True)
        return {"loss": loss}

    def on_validation_start(self) -> None:
        """Calculate the feature map quantiles of the validation dataset and push to the model."""
        map_norm_quantiles = self.map_norm_quantiles(self.trainer.datamodule.val_dataloader())
        self.model.quantiles.update(map_norm_quantiles)

    def validation_step(self, batch: dict[str, str | torch.Tensor], *args, **kwargs) -> STEP_OUTPUT:
        """Perform the validation step of EfficientAd returns anomaly maps for the input image batch.

        Args:
          batch (dict[str, str | torch.Tensor]): Input batch
          args: Additional arguments.
          kwargs: Additional keyword arguments.

        Returns:
          Dictionary containing anomaly maps.
        """
        del args, kwargs  # These variables are not used.

        batch["anomaly_maps"] = self.model(batch["image"])["anomaly_map"]

        return batch

    @property
    def trainer_arguments(self) -> dict[str, Any]:
        """Return EfficientAD trainer arguments."""
        return {"num_sanity_val_steps": 0}

    @property
    def learning_type(self) -> LearningType:
        """Return the learning type of the model.

        Returns:
            LearningType: Learning type of the model.
        """
        return LearningType.ONE_CLASS
