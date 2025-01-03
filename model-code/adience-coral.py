# coding: utf-8

#############################################
# Consistent Cumulative Logits with ResNet-34
#############################################

# Imports

import os
import time
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import argparse
import sys

from torch.utils.data import Dataset
from torch.utils.data import DataLoader

from torchvision import transforms
from PIL import Image

torch.backends.cudnn.deterministic = True

TRAIN_CSV_PATH = '../datasets/adience/train_0.2_3.csv'
VALID_CSV_PATH = '../datasets/adience/valid.csv'
TEST_CSV_PATH = '../datasets/adience/test.csv'
TRAIN_IMAGE_PATH = '../datasets/adience/train'
VALID_IMAGE_PATH = '../datasets/adience/valid'
TEST_IMAGE_PATH = '../datasets/adience/test'


# Argparse helper

parser = argparse.ArgumentParser()
parser.add_argument('--cuda',
                    type=int,
                    default=-1)

parser.add_argument('--seed',
                    type=int,
                    default=-1)

parser.add_argument('--numworkers',
                    type=int,
                    default=3)


parser.add_argument('--outpath',
                    type=str,
                    required=True)

parser.add_argument('--imp_weight',
                    type=int,
                    default=0)

args = parser.parse_args()

NUM_WORKERS = args.numworkers

if args.cuda >= 0:
    DEVICE = torch.device("cuda:%d" % args.cuda)
else:
    DEVICE = torch.device("cpu")

if args.seed == -1:
    RANDOM_SEED = None
else:
    RANDOM_SEED = args.seed

IMP_WEIGHT = args.imp_weight

PATH = args.outpath
if not os.path.exists(PATH):
    os.mkdir(PATH)
LOGFILE = os.path.join(PATH, 'training.log')
TEST_PREDICTIONS = os.path.join(PATH, 'test_predictions.log')
TEST_ALLPROBAS = os.path.join(PATH, 'test_allprobas.tensor')

# Logging

header = []

header.append('PyTorch Version: %s' % torch.__version__)
header.append('CUDA device available: %s' % torch.cuda.is_available())
header.append('Using CUDA device: %s' % DEVICE)
header.append('Random Seed: %s' % RANDOM_SEED)
header.append('Task Importance Weight: %s' % IMP_WEIGHT)
header.append('Output Path: %s' % PATH)
header.append('Script: %s' % sys.argv[0])

with open(LOGFILE, 'w') as f:
    for entry in header:
        print(entry)
        f.write('%s\n' % entry)
        f.flush()


##########################
# SETTINGS
##########################

# Hyperparameters
learning_rate = 0.0005
num_epochs = 200

# Architecture
NUM_CLASSES = 8
BATCH_SIZE = 256
GRAYSCALE = False

df = pd.read_csv(TRAIN_CSV_PATH, index_col=0)
ages = df['age'].values
del df
ages = torch.tensor(ages, dtype=torch.float)


def task_importance_weights(label_array):
    uniq = torch.unique(label_array)
    num_examples = label_array.size(0)

    m = torch.zeros(uniq.shape[0])

    for i, t in enumerate(torch.arange(torch.min(uniq), torch.max(uniq))):
        m_k = torch.max(torch.tensor([label_array[label_array > t].size(0), 
                                      num_examples - label_array[label_array > t].size(0)]))
        m[i] = torch.sqrt(m_k.float())

    imp = m/torch.max(m)
    return imp


# Data-specific scheme
if not IMP_WEIGHT:
    imp = torch.ones(NUM_CLASSES-1, dtype=torch.float)
elif IMP_WEIGHT == 1:
    imp = task_importance_weights(ages)
    imp = imp[0:NUM_CLASSES-1]
else:
    raise ValueError('Incorrect importance weight parameter.')
imp = imp.to(DEVICE)


###################
# Dataset
###################

import os
import pandas as pd
from torch.utils.data import Dataset
from PIL import Image

class AdienceDatasetAge(Dataset):
    """Custom Dataset for loading Adience face images"""

    def __init__(self, csv_path, img_dir, transform=None):
        df = pd.read_csv(csv_path)
        self.img_dir = img_dir
        self.img_paths = df['image_name']
        self.y = df['age'].values
        self.original_age = df['original_age'].values if 'original_age' in df.columns else None
        self.transform = transform

    def __getitem__(self, index):
        # Determine the folder to use for image path
        folder = str(self.original_age[index]) if self.original_age is not None else str(self.y[index])
        
        # Construct the full path to the image
        img_path = os.path.join(self.img_dir, folder, self.img_paths.iloc[index])
        img = Image.open(img_path).convert('RGB')

        if self.transform is not None:
            img = self.transform(img)

        label = self.y[index]
        levels = [1]*label + [0]*(NUM_CLASSES - 1 - label)
        levels = torch.tensor(levels, dtype=torch.float32)

        return img, label, levels

    def __len__(self):
        return len(self.y)


custom_transform = transforms.Compose([transforms.Resize((128, 128)),
                                       transforms.RandomCrop((120, 120)),
                                       transforms.ToTensor()])

train_dataset = AdienceDatasetAge(csv_path=TRAIN_CSV_PATH,
                               img_dir=TRAIN_IMAGE_PATH,
                               transform=custom_transform)


custom_transform2 = transforms.Compose([transforms.Resize((128, 128)),
                                        transforms.CenterCrop((120, 120)),
                                        transforms.ToTensor()])

test_dataset = AdienceDatasetAge(csv_path=TEST_CSV_PATH,
                              img_dir=TEST_IMAGE_PATH,
                              transform=custom_transform2)

valid_dataset = AdienceDatasetAge(csv_path=VALID_CSV_PATH,
                               img_dir=VALID_IMAGE_PATH,
                               transform=custom_transform2)


train_loader = DataLoader(dataset=train_dataset,
                          batch_size=BATCH_SIZE,
                          shuffle=True,
                          num_workers=NUM_WORKERS)

valid_loader = DataLoader(dataset=valid_dataset,
                          batch_size=BATCH_SIZE,
                          shuffle=False,
                          num_workers=NUM_WORKERS)
test_loader = DataLoader(dataset=test_dataset,
                         batch_size=BATCH_SIZE,
                         shuffle=False,
                         num_workers=NUM_WORKERS)


##########################
# MODEL
##########################


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class ResNet(nn.Module):

    def __init__(self, block, layers, num_classes, grayscale):
        self.num_classes = num_classes
        self.inplanes = 64
        if grayscale:
            in_dim = 1
        else:
            in_dim = 3
        super(ResNet, self).__init__()
        self.conv1 = nn.Conv2d(in_dim, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.avgpool = nn.AvgPool2d(4)
        self.fc = nn.Linear(512, 1, bias=False)
        self.linear_1_bias = nn.Parameter(torch.zeros(self.num_classes-1).float())

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, (2. / n)**.5)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        logits = self.fc(x)
        logits = logits + self.linear_1_bias
        probas = torch.sigmoid(logits)
        return logits, probas


def resnet34(num_classes, grayscale):
    """Constructs a ResNet-34 model."""
    model = ResNet(block=BasicBlock,
                   layers=[3, 4, 6, 3],
                   num_classes=num_classes,
                   grayscale=grayscale)
    return model


###########################################
# Initialize Cost, Model, and Optimizer
###########################################

def cost_fn(logits, levels, imp):
    val = (-torch.sum((F.logsigmoid(logits)*levels
                      + (F.logsigmoid(logits) - logits)*(1-levels))*imp,
           dim=1))
    return torch.mean(val)


torch.manual_seed(RANDOM_SEED)
torch.cuda.manual_seed(RANDOM_SEED)
model = resnet34(NUM_CLASSES, GRAYSCALE)

model.to(DEVICE)
optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate) 


def compute_metrics(model, data_loader, device, cost_fn, imp):
    mae, mse, loss, correct, num_examples = 0, 0, 0, 0, 0
    for i, (features, targets, levels) in enumerate(data_loader):
        features = features.to(device)
        targets = targets.to(device)
        levels = levels.to(device)

        logits, probas = model(features)
        predict_levels = probas > 0.5
        predicted_labels = torch.sum(predict_levels, dim=1)

        # Compute MAE and MSE
        num_examples += targets.size(0)
        mae += torch.sum(torch.abs(predicted_labels - targets))
        mse += torch.sum((predicted_labels - targets)**2)

        # Compute Loss
        cost = cost_fn(logits, levels, imp)
        loss += cost.item() * features.size(0)

        # Compute Accuracy
        correct += torch.sum(predicted_labels == targets)

    mae = mae.float() / num_examples
    mse = mse.float() / num_examples
    loss = loss / num_examples
    accuracy = (correct.float() / num_examples) * 100
    return mae, torch.sqrt(mse), loss, accuracy


start_time = time.time()

best_mae, best_rmse, best_loss, best_accuracy, best_epoch = 999, 999, 999, 0, -1
for epoch in range(num_epochs):

    model.train()
    for batch_idx, (features, targets, levels) in enumerate(train_loader):

        features = features.to(DEVICE)
        targets = targets.to(DEVICE)
        levels = levels.to(DEVICE)

        # FORWARD AND BACK PROP
        logits, probas = model(features)
        cost = cost_fn(logits, levels, imp)
        optimizer.zero_grad()
        cost.backward()

        # UPDATE MODEL PARAMETERS
        optimizer.step()

        # LOGGING
        if not batch_idx % 10:
            s = ('Epoch: %03d/%03d | Batch %04d/%04d | Cost: %.4f'
                 % (epoch+1, num_epochs, batch_idx,
                     len(train_dataset)//BATCH_SIZE, cost))
            print(s)
            with open(LOGFILE, 'a') as f:
                f.write('%s\n' % s)

    model.eval()
    with torch.set_grad_enabled(False):
        valid_mae, valid_rmse, valid_loss, valid_accuracy = compute_metrics(
            model, valid_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)

    if valid_mae < best_mae:
        best_mae, best_rmse, best_loss, best_accuracy, best_epoch = valid_mae, valid_rmse, valid_loss, valid_accuracy ,epoch
        ########## SAVE MODEL #############
        torch.save(model.state_dict(), os.path.join(PATH, 'best_model.pt'))

    s = 'MAE/RMSE/Loss/Accuracy: | Current Valid: %.2f/%.2f/%.2f/%.2f%% Ep. %d | Best Valid : %.2f/%.2f/%.2f/%.2f%% Ep. %d' % (
        valid_mae, valid_rmse, valid_loss, valid_accuracy, epoch+1, best_mae, best_rmse, best_loss, best_accuracy, best_epoch+1)
    print(s)
    with open(LOGFILE, 'a') as f:
        f.write('%s\n' % s)

    s = 'Time elapsed: %.2f min' % ((time.time() - start_time)/60)
    print(s)
    with open(LOGFILE, 'a') as f:
        f.write('%s\n' % s)

model.eval()
with torch.set_grad_enabled(False):  # save memory during inference
    train_mae, train_rmse, train_loss, train_accuracy = compute_metrics(
        model, train_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)
    valid_mae, valid_rmse, valid_loss, valid_accuracy = compute_metrics(
        model, valid_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)
    test_mae, test_rmse, test_loss, test_accuracy = compute_metrics(
        model, test_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)

    s = '\nMAE/RMSE/Loss/Accuracy: | Last Train: %.2f/%.2f/%.2f/%.2f%% | Last Valid: %.2f/%.2f/%.2f/%.2f%% | Last Test: %.2f/%.2f/%.2f/%.2f%%' % (
        train_mae, train_rmse, train_loss, train_accuracy,
        valid_mae, valid_rmse, valid_loss, valid_accuracy,
        test_mae, test_rmse, test_loss, test_accuracy)
    print(s)
    with open(LOGFILE, 'a') as f:
        f.write('%s\n' % s)

s = 'Total Training Time: %.2f min' % ((time.time() - start_time)/60)
print(s)
with open(LOGFILE, 'a') as f:
    f.write('%s\n' % s)


########## EVALUATE BEST MODEL ######
model.load_state_dict(torch.load(os.path.join(PATH, 'best_model.pt'), weights_only=True))
model.eval()

with torch.set_grad_enabled(False):
    train_mae, train_rmse, train_loss, train_accuracy = compute_metrics(
        model, train_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)
    valid_mae, valid_rmse, valid_loss, valid_accuracy = compute_metrics(
        model, valid_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)
    test_mae, test_rmse, test_loss, test_accuracy = compute_metrics(
        model, test_loader, device=DEVICE, cost_fn=cost_fn, imp=imp)

    s = '\nMAE/RMSE/Loss/Accuracy: | Best Train: %.2f/%.2f/%.2f/%.2f%% | Best Valid: %.2f/%.2f/%.2f/%.2f%% | Best Test: %.2f/%.2f/%.2f/%.2f%%' % (
        train_mae, train_rmse, train_loss, train_accuracy,
        valid_mae, valid_rmse, valid_loss, valid_accuracy,
        test_mae, test_rmse, test_loss, test_accuracy)
    print(s)
    with open(LOGFILE, 'a') as f:
        f.write('%s\n' % s)

########## SAVE PREDICTIONS ######
all_pred = []
all_probas = []
with torch.set_grad_enabled(False):
    for batch_idx, (features, targets, levels) in enumerate(test_loader):
        
        features = features.to(DEVICE)
        logits, probas = model(features)
        all_probas.append(probas)
        predict_levels = probas > 0.5
        predicted_labels = torch.sum(predict_levels, dim=1)
        lst = [str(int(i)) for i in predicted_labels]
        all_pred.extend(lst)

torch.save(torch.cat(all_probas).to(torch.device('cpu')), TEST_ALLPROBAS)
with open(TEST_PREDICTIONS, 'w') as f:
    all_pred = ','.join(all_pred)
    f.write(all_pred)
