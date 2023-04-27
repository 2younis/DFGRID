import mlflow
import torch
import torch.nn.functional as F
from configs import config
from dataset.dataset import (
    TestDatasetComplete,
    TrainDatasetComplete,
    TrainDatasetUnbalanced,
)
from models.classifier import Classifier
from models.generator import Generator
from torch import nn
from torch.utils.data import DataLoader
from utils.loss import focal_loss, js_divergence, merge_gaussians


cfg = config.cfg("configs/config.yaml")


generator = Generator(cfg).to(cfg["device"])

g_optimizer = torch.optim.Adam(
    generator.parameters(),
    lr=cfg["gen_optimizer_lr"],
    betas=(cfg["gen_optimizer_beta_1"], cfg["optimizer_beta_2"]),
)


classifier = Classifier(cfg).to(cfg["device"])

cl_optimizer = torch.optim.Adam(
    classifier.parameters(),
    lr=cfg["cl_optimizer_lr"],
    betas=(cfg["cl_optimizer_beta_1"], cfg["optimizer_beta_2"]),
)


def cumulative(lists):
    cu_list = []
    length = len(lists)
    cu_list = [sum(lists[0:x:1]) for x in range(0, length + 1)]
    return cu_list[1:]


def train_classifier(task):

    replay_tasks = {}

    if cfg["classifier_checkpoint_path"].is_file():
        model_checkpoint = torch.load(cfg["classifier_model_file"])
        classifier.load_state_dict(model_checkpoint["model"])
        classifier.to(cfg["device"])

    if cfg["generator_checkpoint_path"].is_file():
        model_checkpoint = torch.load(cfg["generator_model_file"])
        generator.load_state_dict(model_checkpoint["model"])
        replay_tasks = model_checkpoint["replay_tasks"]
        generator.to(cfg["device"])
        generator.eval()

    best_loss = float(cfg["max_loss"])
    patience_counter = 0

    probabilities = None

    classifier.train()

    if not replay_tasks:
        mix_ratio = 0
    else:
        mix_ratio = len(replay_tasks) / (len(replay_tasks) + len(task))

    dataset = TrainDatasetUnbalanced(cfg, task, dataset=cfg["image_dataset"])

    dataloader = DataLoader(
        dataset, batch_size=cfg["cl_batch_size"], shuffle=True, drop_last=True
    )

    mlflow.log_param("classifier replay task", replay_tasks)
    mlflow.log_param("mix ratio", mix_ratio)

    dataset_len = len(dataset)

    for epoch in range(cfg["max_epochs"]):
        train_loss = 0
        replay_losses = 0
        real_losses = 0

        replay_epoch_losses = None
        replay_labels = None
        for imgs, labels in dataloader:

            imgs, labels = imgs.to(cfg["device"]), labels.to(cfg["device"])

            cl_optimizer.zero_grad()
            output, _ = classifier(imgs)

            real_loss = focal_loss(cfg, output, labels)

            if mix_ratio > 0:
                with torch.no_grad():
                    gen_imgs, gen_labels = generator.generate(
                        cfg["cl_batch_size"],
                        replay_tasks,
                        trunc=cfg["truncation"],
                        probabilities=probabilities,
                    )
                gen_imgs, gen_labels = (
                    gen_imgs.to(cfg["device"]).detach(),
                    gen_labels.to(cfg["device"]).detach(),
                )

                gen_output, _ = classifier(gen_imgs)
                replay_loss = F.cross_entropy(gen_output, gen_labels, reduction="none")

                if replay_labels is None:
                    replay_labels = gen_labels.detach().cpu()
                    replay_epoch_losses = replay_loss.detach().cpu()
                else:
                    replay_labels = torch.cat(
                        [replay_labels, gen_labels.detach().cpu()], dim=0
                    )
                    replay_epoch_losses = torch.cat(
                        [replay_epoch_losses, replay_loss.detach().cpu()], dim=0
                    )

                replay_loss = replay_loss.mean()

                loss = (1 - mix_ratio) * real_loss + mix_ratio * replay_loss
                replay_losses += replay_loss.item()

            else:

                loss = real_loss

            loss.backward()
            cl_optimizer.step()
            train_loss += loss.item()
            real_losses += real_loss.item()

        if replay_labels is not None:

            probabilities = adjust_replay_probabilities(
                replay_epoch_losses, replay_labels, probabilities
            )

        train_loss /= dataset_len / cfg["cl_batch_size"]
        real_losses /= dataset_len / cfg["cl_batch_size"]
        replay_losses /= dataset_len / cfg["cl_batch_size"]

        mlflow.log_metric("Total Loss", train_loss, step=epoch)
        mlflow.log_metric("Real Loss", real_losses, step=epoch)
        mlflow.log_metric("Replay Loss", replay_losses, step=epoch)

        if train_loss < best_loss:
            best_loss = train_loss
            patience_counter = 0
            model_checkpoint = {
                "model": classifier.state_dict(),
                "trained_tasks": task | replay_tasks,
                "batch_size": cfg["cl_batch_size"],
            }
            torch.save(model_checkpoint, cfg["classifier_model_file"])
        else:
            patience_counter += 1

        mlflow.log_metric("Best loss", best_loss, step=epoch)

        if patience_counter >= cfg["cl_max_patience"]:
            break

    save_features(cfg)


def train_generator(task, generator_params):

    delta, alpha, beta, gamma = generator_params

    replay_tasks = {}

    if cfg["classifier_checkpoint_path"].is_file():
        model_checkpoint = torch.load(cfg["classifier_model_file"])
        classifier.load_state_dict(model_checkpoint["model"])
        classifier.to(cfg["device"])

    if cfg["generator_checkpoint_path"].is_file():
        model_checkpoint = torch.load(cfg["generator_model_file"])
        generator.load_state_dict(model_checkpoint["model"])
        replay_tasks = model_checkpoint["replay_tasks"]
        generator.to(cfg["device"])

        task |= replay_tasks

        mlflow.log_param("generator replay task", task)

    mlflow.log_param("generator task", task)
    mlflow.log_param("delta", delta)
    mlflow.log_param("alpha", alpha)
    mlflow.log_param("beta", beta)
    mlflow.log_param("gamma", gamma)

    classifier.eval()
    classifier.register_hooks()

    best_loss = float(cfg["max_loss"])
    patience_counter = 0

    batchnorm_means, batchnorm_vars = None, None

    for module in classifier.modules():
        if isinstance(module, nn.BatchNorm2d):

            if batchnorm_means is None:
                batchnorm_means = module.running_mean
                batchnorm_vars = module.running_var
            else:
                batchnorm_means = torch.cat(
                    [batchnorm_means, module.running_mean], dim=0
                )
                batchnorm_vars = torch.cat([batchnorm_vars, module.running_var], dim=0)

    batchnorm_means = batchnorm_means[cfg["img_channels"] :]
    batchnorm_vars = batchnorm_vars[cfg["img_channels"] :]

    features_dict = torch.load(cfg["features_file"])

    dataset_len = len(task) * cfg["images_per_task"]
    gen_batches = int(dataset_len / cfg["gen_batch_size"])

    for epoch in range(cfg["max_epochs"]):
        div_losses = 0
        features_losses = 0
        batchmorm_losses = 0
        class_losses = 0
        g_losses = 0

        generator.train()
        classifier.eval()

        for _ in range(gen_batches):

            g_optimizer.zero_grad()

            fakes, labels = generator.generate(cfg["gen_batch_size"], task)
            fakes, labels = fakes.to(cfg["device"]), labels.to(cfg["device"])

            sigma_1, mu1 = merge_gaussians(features_dict, labels)

            output_fake, fake_h = classifier(fakes)
            class_loss = F.cross_entropy(output_fake, labels)

            sigma_2, mu2 = torch.var_mean(fake_h, dim=0, unbiased=False)

            batch_means, batch_vars = None, None

            for hook in classifier.hooks:
                if hook.mean is not None:
                    if batch_means is None:
                        batch_means = hook.mean
                        batch_vars = hook.var
                    else:
                        batch_means = torch.cat([batch_means, hook.mean], dim=0)
                        batch_vars = torch.cat([batch_vars, hook.var], dim=0)

            features_loss = torch.norm(mu1.to(cfg["device"]) - mu2) + torch.norm(
                sigma_1.to(cfg["device"]) - sigma_2
            )
            batchmorm_loss = torch.norm(batchnorm_means - batch_means) + torch.norm(
                batchnorm_vars - batch_vars
            )

            div_loss = -js_divergence(fakes, cfg["num_div_samples"])

            g_loss = (
                (delta * class_loss)
                + (alpha * features_loss)
                + (beta * batchmorm_loss)
                + (gamma * div_loss)
            )

            g_loss.backward()
            g_optimizer.step()

            class_losses += class_loss.item()
            features_losses += features_loss.item()
            batchmorm_losses += batchmorm_loss.item()
            div_losses += div_loss.item()
            g_losses += g_loss.item()

        div_losses /= dataset_len / cfg["gen_batch_size"]
        class_losses /= dataset_len / cfg["gen_batch_size"]
        features_losses /= dataset_len / cfg["gen_batch_size"]
        batchmorm_losses /= dataset_len / cfg["gen_batch_size"]
        g_losses /= dataset_len / cfg["gen_batch_size"]

        mlflow.log_metric("divergence loss", div_losses, step=epoch)
        mlflow.log_metric("classification loss", class_losses, step=epoch)
        mlflow.log_metric("features loss", features_losses, step=epoch)
        mlflow.log_metric("batchmorm loss", batchmorm_losses, step=epoch)
        mlflow.log_metric("Total loss", g_losses, step=epoch)

        if g_losses < best_loss:
            best_loss = g_losses
            patience_counter = 0
            model_checkpoint = {
                "model": generator.state_dict(),
                "replay_tasks": task,
                "batch_size": cfg["gen_batch_size"],
            }
            torch.save(model_checkpoint, cfg["generator_model_file"])
        else:
            patience_counter += 1

        mlflow.log_metric("Best loss", best_loss, step=epoch)

        if patience_counter >= cfg["gen_max_patience"]:
            break


def validate_classifier():

    trained_tasks = {}

    if cfg["classifier_checkpoint_path"].is_file():
        model_checkpoint = torch.load(cfg["classifier_model_file"])
        classifier.load_state_dict(model_checkpoint["model"])
        trained_tasks = model_checkpoint["trained_tasks"]
        classifier.to(cfg["device"])

    classifier.eval()

    mlflow.log_param("validation task", trained_tasks)

    overall_correct = 0
    overall_total = 0

    for task_id, task_probability in trained_tasks.items():

        dataset = TrainDatasetUnbalanced(
            cfg, {task_id: task_probability}, dataset=cfg["image_dataset"]
        )

        dataloader = DataLoader(dataset, batch_size=cfg["val_batch_size"])

        total = len(dataset)
        overall_total += total

        valid_loss = 0
        correct = 0

        with torch.no_grad():
            for imgs, labels in dataloader:

                imgs, labels = imgs.to(cfg["device"]), labels.to(cfg["device"])
                output, _ = classifier(imgs)

                valid_loss += F.cross_entropy(output, labels).item()
                max_indices = output.max(1)[1]
                correct += (max_indices == labels).sum().detach().item()

                valid_loss /= total / cfg["val_batch_size"]

        overall_correct += correct

        mlflow.log_metric("loss task id " + str(task_id), valid_loss)
        mlflow.log_metric("accuracy task id " + str(task_id), 100.0 * correct / total)

    mlflow.log_metric("overall accuracy", 100.0 * overall_correct / overall_total)


def save_features(cfg):

    if cfg["classifier_checkpoint_path"].is_file():
        model_checkpoint = torch.load(cfg["classifier_model_file"])
        classifier.load_state_dict(model_checkpoint["model"])
        trained_tasks = model_checkpoint["trained_tasks"]
        classifier.to(cfg["device"])

    classifier.eval()

    dataset = TrainDatasetUnbalanced(cfg, trained_tasks, dataset=cfg["image_dataset"])
    dataloader = DataLoader(dataset, batch_size=cfg["val_batch_size"], shuffle=True)

    features = None
    classes = None

    with torch.no_grad():
        for imgs, labels in dataloader:

            imgs, labels = imgs.to(cfg["device"]), labels.to(cfg["device"])
            _, h = classifier(imgs)

            if features is None:
                features = h.detach().cpu()
                classes = labels.detach().cpu()
            else:
                features = torch.cat([features, h.detach().cpu()], dim=0)
                classes = torch.cat([classes, labels.detach().cpu()], dim=0)

    values, indices = torch.sort(classes)
    bin_count = torch.bincount(classes).tolist()

    bin_count = [i for i in bin_count if i != 0]
    bin_count.insert(0, 0)
    bin_count_cum = cumulative(bin_count)

    clss = [int(classes[indices[bin_count_cum[a]]]) for a in range(len(bin_count) - 1)]

    mean = torch.empty((len(clss), features.shape[1]))
    var = torch.empty((len(clss), features.shape[1]))

    for a in range(len(bin_count) - 1):

        mean[a] = torch.mean(
            features[indices[bin_count_cum[a] : bin_count_cum[a + 1]]], dim=0
        )

        var[a] = torch.var(
            features[indices[bin_count_cum[a] : bin_count_cum[a + 1]]], dim=0
        )

    features_dict = {"mean": mean, "var": var, "labels": clss}
    torch.save(features_dict, cfg["features_file"])


def adjust_replay_probabilities(loss, labels, p):

    replay_labels = list(set(labels.tolist()))

    if p is None:
        len_labels = len(replay_labels)
        p = [1.0 / len_labels] * len_labels
        return p

    _, indices = torch.sort(labels)
    bin_count = torch.bincount(labels).tolist()

    bin_count = [i for i in bin_count if i != 0]

    bin_count.insert(0, 0)
    bin_count_cum = cumulative(bin_count)

    avg_losses = [
        torch.mean(loss[indices[bin_count_cum[a] : bin_count_cum[a + 1]]]).item()
        for a in range(len(bin_count) - 1)
    ]

    normalized_avg_losses = [a / bin_count[i + 1] for i, a in enumerate(avg_losses)]

    mean_p = sum(p) / len(p)
    p = [(x * y) for x, y in zip(p, normalized_avg_losses)]
    mean_p = sum(p) / len(p)
    p = [a + mean_p for a in p]
    p = [a / sum(p) for a in p]

    return p