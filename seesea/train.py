"Training script for the SeeSea model."

import os
import logging
import datetime
import json
from dataclasses import dataclass, asdict
from typing import List, Tuple, Callable

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import transforms, models
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from tqdm import tqdm

import seesea.utils as utils
from seesea.observation import Observation, ImageObservation
from seesea.seesea_dataset import SeeSeaDataset

LOGGER = logging.getLogger(__name__)


@dataclass
class TrainingDetails:
    model: str
    output_name: str
    epochs: int
    batch_size: int
    learning_rate: float
    training_start_time: str
    training_end_time: str
    train_losses: List[float]
    val_losses: List[float]

    def to_dict(self):
        """Convert the dataclass to a dictionary"""
        return asdict(self)


def train_one_epoch(model, criterion, optimizer, loader, device):
    """Train the model for one epoch"""
    model.train()
    running_loss = 0.0

    input_processed = 0
    for inputs, label in tqdm(loader, leave=False, desc="Training", disable=LOGGER.level > logging.INFO):
        # pring the percentage of the dataset that has been processed

        inputs = inputs.to(device)
        label = label.to(device)

        # Zero the parameter gradients
        optimizer.zero_grad()

        # Forward pass
        outputs = model(inputs)
        outputs = outputs.view(-1)  # Flatten outputs to match wind_speeds shape
        loss = criterion(outputs, label.view(-1))

        # Backward pass and optimization
        loss.backward()
        optimizer.step()

        running_loss += loss.item() * inputs.size(0)
        input_processed += inputs.size(0)

    assert input_processed == len(loader.dataset)

    return running_loss / len(loader.dataset)


def evaluate_model(model, criterion, loader, device):
    """Evaluate the model"""
    model.eval()
    running_loss = 0.0

    with torch.no_grad():
        for inputs, label in tqdm(loader, leave=False, desc="Validation", disable=LOGGER.level > logging.INFO):
            inputs = inputs.to(device)
            label = label.to(device)

            outputs = model(inputs)
            outputs = outputs.view(-1)
            loss = criterion(outputs, label.view(-1))

            running_loss += loss.item() * inputs.size(0)

    return running_loss / len(loader.dataset)


def main(args):
    # train the model
    LOGGER.info("Training the model to classify %s", args.output_name)

    if not os.path.exists(args.output):
        os.makedirs(args.output)

    train_file = os.path.join(args.input, "train.json")
    val_file = os.path.join(args.input, "val.json")

    model, transform = utils.continuous_single_output_model_factory(args.model)

    train_dataset = SeeSeaDataset(train_file, observation_key=args.output_name, transform=transform)
    val_dataset = SeeSeaDataset(val_file, observation_key=args.output_name, transform=transform)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=4)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=4)

    if torch.cuda.is_available():
        device = torch.device("cuda")
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    model = model.to(device)

    # Loss Function and Optimizer
    criterion = nn.MSELoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)

    training_start_time = datetime.datetime.now(tz=datetime.timezone.utc)

    train_losses = []
    val_losses = []
    # Training Loop
    for epoch in range(args.epochs):

        LOGGER.debug("Starting epoch %d/%d", epoch + 1, args.epochs)
        # Training Phase
        epoch_loss = train_one_epoch(model, criterion, optimizer, train_loader, device)
        train_losses.append(epoch_loss)
        LOGGER.info("Epoch %d/%d, Training Loss: %.4f", epoch + 1, args.epochs, epoch_loss)

        # Validation Phase
        val_epoch_loss = evaluate_model(model, criterion, val_loader, device)
        val_losses.append(val_epoch_loss)
        LOGGER.info("Epoch %d/%d, Validation Loss: %.4f", epoch + 1, args.epochs, val_epoch_loss)

    training_end_time = datetime.datetime.now(tz=datetime.timezone.utc)
    LOGGER.info("Training complete. Training time: %s", training_end_time - training_start_time)

    # Save the model
    timestamp_str = training_start_time.strftime("%Y_%m_%d_%H%M")
    model_dir = os.path.join(args.output, timestamp_str)

    if not os.path.exists(model_dir):
        os.mkdir(model_dir)

    model_filepath = os.path.join(model_dir, "model.pth")
    torch.save(model.state_dict(), model_filepath)

    # Plot the training and validation loss
    plt.plot(train_losses, label="Training Loss")
    plt.plot(val_losses, label="Validation Loss")
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.legend()

    loss_plot_filepath = os.path.join(model_dir, "loss_plot.png")
    plt.savefig(loss_plot_filepath)

    training_details = TrainingDetails(
        model=args.model,
        output_name=args.output_name,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        training_start_time=training_start_time.isoformat(),
        training_end_time=training_end_time.isoformat(),
        train_losses=train_losses,
        val_losses=val_losses,
    )

    with open(os.path.join(model_dir, "training_details.json"), "w", encoding="utf-8") as f:
        json.dump(training_details.to_dict(), f, indent=4)

    LOGGER.info("Output saved to %s", model_dir)


def get_args_parser():
    import argparse

    parser = argparse.ArgumentParser(description="Train the SeeSea model")
    parser.add_argument("--input", help="The directory containing the training data", default="data")
    parser.add_argument("--output", help="The directory to write the output files to", default="data/train")
    parser.add_argument("--log", type=str, help="Log level", default="INFO")
    parser.add_argument("--log-file", type=str, help="Log file", default=None)
    parser.add_argument("--epochs", type=int, help="The number of epochs to train for", default=30)
    parser.add_argument("--batch-size", type=int, help="The batch size to use for training", default=32)
    parser.add_argument("--learning-rate", type=float, help="The learning rate to use for training", default=0.001)
    parser.add_argument("--model", type=str, help="The model to use for training", default="resnet18")
    parser.add_argument(
        "--output-name",
        type=str,
        help="The observation variable to train the netowrk to classify",
        default="wind_speed_mps",
    )
    parser.add_argument("--model-path", type=str, help="The path to save the trained model", default="model.pth")
    return parser


if __name__ == "__main__":

    parser = get_args_parser()

    args = parser.parse_args()

    # setup the loggers
    LOGGER.setLevel(args.log)

    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_logging_handler = logging.StreamHandler()
    console_logging_handler.setFormatter(log_formatter)
    LOGGER.addHandler(console_logging_handler)

    if args.log_file is not None:
        file_logging_handler = logging.FileHandler(args.log_file)
        file_logging_handler.setFormatter(log_formatter)
        LOGGER.addHandler(file_logging_handler)

    main(args)
