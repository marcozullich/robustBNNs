import argparse
import os
from directories import *
from utils import *
import pyro
import torch
from torch import nn
import torch.nn.functional as nnf
import numpy as np
from pyro.infer import SVI, Trace_ELBO, TraceMeanField_ELBO, Predictive
import torch.optim as torchopt
from pyro import poutine
import pyro.optim as pyroopt
import torch.nn.functional as F
from utils import plot_loss_accuracy
import torch.distributions.constraints as constraints
softplus = torch.nn.Softplus()
from pyro.nn import PyroModule
from bnn import BNN
import pandas
import itertools
from lossGradients import loss_gradients, load_loss_gradients
import matplotlib
from adversarialAttacks import attack, attack_evaluation, load_attack


DATA=DATA+"half_moons_grid_search/"
ACC_THS=80

#################################
# exp loss gradients components #
#################################

class MoonsBNN(BNN):
    def __init__(self, hidden_size, activation, architecture, inference, 
                 epochs, lr, n_samples, warmup, n_inputs, input_shape, output_size):
        super(MoonsBNN, self).__init__("half_moons", hidden_size, activation, architecture, 
                inference, epochs, lr, n_samples, warmup, input_shape, output_size)
        self.name = self.get_name(epochs, lr, n_samples, warmup, n_inputs)
        print(self.inference)

def plot_half_moons(n_points=200):

    x_train, y_train, x_test, y_test, inp_shape, out_size = \
        load_dataset(dataset_name="half_moons", n_inputs=n_points, channels="first") 
    
    labels = onehot_to_labels(y_train)
    sns.set_style("darkgrid")
    fig, ax = plt.subplots(nrows=1, ncols=1, figsize=(10, 6), dpi=150, facecolor='w', edgecolor='k')
    df = pandas.DataFrame.from_dict({"x":x_train.squeeze()[:,0],
                                     "y":x_train.squeeze()[:,1],
                                     "label":labels[:]})
    g = sns.scatterplot(data=df, x="x", y="y", hue="label", alpha=0.9, ax=ax)
    filename = "halfMoons_"+str(n_points)+".png"
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    plt.savefig(TESTS + filename)

def _train(hidden_size, activation, architecture, inference, 
           epochs, lr, n_samples, warmup, n_inputs, posterior_samples, device):

    train_loader, _, inp_shape, out_size = \
            data_loaders(dataset_name="half_moons", batch_size=64, n_inputs=n_inputs, shuffle=False)

    bnn = MoonsBNN(hidden_size, activation, architecture, inference, 
                   epochs, lr, n_samples, warmup, n_inputs, inp_shape, out_size)
    bnn.train(train_loader=train_loader, device=device)

def serial_train(hidden_size, activation, architecture, inference, 
                         epochs, lr, n_samples, warmup, n_inputs, posterior_samples):

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                         epochs, lr, n_samples, warmup, n_inputs, posterior_samples))
    
    for init in combinations:
        _train(*init, "cuda")

def parallel_train(hidden_size, activation, architecture, inference, 
                         epochs, lr, n_samples, warmup, n_inputs, posterior_samples):
    from joblib import Parallel, delayed

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                         epochs, lr, n_samples, warmup, n_inputs, posterior_samples))
    
    Parallel(n_jobs=10)(
        delayed(_train)(*init, "cpu") for init in combinations)

def _compute_grads(hidden_size, activation, architecture, inference, 
           epochs, lr, n_samples, warmup, n_inputs, posterior_samples, rel_path, test_points):

    _, test_loader, inp_shape, out_size = \
        data_loaders(dataset_name="half_moons", batch_size=64, n_inputs=test_points, shuffle=True)

    bnn = MoonsBNN(hidden_size, activation, architecture, inference, 
                   epochs, lr, n_samples, warmup, n_inputs, inp_shape, out_size)
    bnn.load(device="cpu", rel_path=rel_path)
    loss_gradients(net=bnn, n_samples=posterior_samples, savedir=bnn.name+"/", 
                    data_loader=test_loader, device="cpu", filename=bnn.name)

def parallel_compute_grads(hidden_size, activation, architecture, inference, 
                         epochs, lr, n_samples, warmup, n_inputs, posterior_samples, 
                          rel_path, test_points):
    from joblib import Parallel, delayed

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                         epochs, lr, n_samples, warmup, n_inputs, posterior_samples))
    
    Parallel(n_jobs=10)(
        delayed(_compute_grads)(*init, rel_path, test_points) for init in combinations)


def build_components_dataset(hidden_size, activation, architecture, inference, epochs, lr, 
                            n_samples,  warmup, n_inputs, posterior_samples, test_points,
                            device="cuda", rel_path=TESTS):

    _, _, x_test, y_test, inp_shape, out_size = \
        load_dataset(dataset_name="half_moons", n_inputs=test_points, channels="first") 

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                                          epochs, lr, n_samples, warmup, n_inputs))
    
    cols = ["hidden_size", "activation", "architecture", "inference", "epochs", "lr", 
            "n_samples", "warmup", "n_inputs", "posterior_samples", "test_acc",
            "x","y","loss_gradients_x","loss_gradients_y"]
    df = pandas.DataFrame(columns=cols)

    row_count = 0
    for init in combinations:

        bnn = MoonsBNN(*init, inp_shape, out_size)
        bnn.load(device=device, rel_path=rel_path)
        
        for p_samp in posterior_samples:
            bnn_dict = {cols[k]:val for k, val in enumerate(init)}

            test_loader = DataLoader(dataset=list(zip(x_test, y_test)), batch_size=64)
            test_acc = bnn.evaluate(test_loader=test_loader, device=device, n_samples=p_samp)
            loss_grads = load_loss_gradients(n_samples=p_samp, filename=bnn.name, 
                                             savedir=bnn.name+"/", relpath=rel_path)

            loss_gradients_components = loss_grads[:test_points]
            for idx, grad in enumerate(loss_gradients_components):
                x, y = x_test[idx].squeeze()
                bnn_dict.update({"posterior_samples":p_samp, "test_acc":test_acc,
                                 "x":x,"y":y,
                                 "loss_gradients_x":grad[0], "loss_gradients_y":grad[1]})
                df.loc[row_count] = pandas.Series(bnn_dict)
                row_count += 1

    print("\nSaving:", df.head())
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    df.to_csv(TESTS+"halfMoons_lossGrads_gridSearch_"+str(test_points)+".csv", 
              index = False, header=True)
    return df

def scatterplot_gridSearch_samp_vs_hidden(dataset, posterior_samples, hidden_size, 
                                           test_points, device="cuda"):

    dataset = dataset[dataset["test_acc"]>ACC_THS]
    print("\n---scatterplot_gridSearch_samp_vs_hidden---\n", dataset)

    categorical_rows = dataset["hidden_size"]
    categorical_cols = dataset["posterior_samples"]
    nrows = len(hidden_size)
    ncols = len(posterior_samples)

    sns.set_style("darkgrid")
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("", ["orangered","darkred","black"])
    matplotlib.rc('font', **{'size': 10})
    fig, ax = plt.subplots(nrows=nrows, ncols=ncols, figsize=(10, 6), dpi=150, 
                           facecolor='w', edgecolor='k')

    min_acc, max_acc = dataset["test_acc"].min(), dataset["test_acc"].max()

    for r, row_val in enumerate(hidden_size):
        for c, col_val in enumerate(posterior_samples):

            df = dataset[(categorical_rows==row_val)&(categorical_cols==col_val)]
            g = sns.scatterplot(data=df, x="loss_gradients_x", y="loss_gradients_y", 
                                size="test_acc", hue="test_acc", alpha=0.8, 
                                vmin=min_acc, vmax=max_acc, 
                                ax=ax[r,c], legend=False, sizes=(20, 80), palette=cmap)
            ax[r,c].set_xlabel("")
            ax[r,c].set_ylabel("")
            xlim=np.max(np.abs(df["loss_gradients_x"]))+2
            ylim=np.max(np.abs(df["loss_gradients_y"]))+2
            # ax[r,c].set_xlim(-xlim,+xlim)
            # ax[r,c].set_ylim(-ylim,+ylim)

            # ax[0,c].xaxis.set_label_position("top")
            # ax[r,-1].yaxis.set_label_position("right")
            ax[-1,c].set_xlabel(str(col_val),labelpad=3,fontdict=dict(weight='bold'))
            ax[r,0].set_ylabel(str(row_val),labelpad=10,fontdict=dict(weight='bold')) 

    ## colorbar    
    cbar_ax = fig.add_axes([0.93, 0.08, 0.01, 0.8])
    cbar = fig.colorbar(matplotlib.cm.ScalarMappable(norm=None, cmap=cmap), cax=cbar_ax)
    cbar.ax.set_ylabel('Test accuracy (%)', rotation=270, fontdict=dict(weight='bold'))
    cbar.set_ticks([0,1])
    cbar.set_ticklabels([ACC_THS,100])
    
    ## titles and labels
    fig.text(0.03, 0.5, "Hidden size", va='center',fontsize=12, rotation='vertical',
        fontdict=dict(weight='bold'))
    fig.text(0.5, 0.01, r"Samples involved in the expectations ($w \sim p(w|D)$)", 
        fontsize=12, ha='center',fontdict=dict(weight='bold'))
    fig.suptitle(r"Expected loss gradients components $\langle \nabla_{x} L(x,w)\rangle_{w}$ on Half Moons dataset",
               fontsize=12, ha='center', fontdict=dict(weight='bold'))

    filename = "halfMoons_samp_vs_hidden_"+str(test_points)+".png"
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    plt.savefig(TESTS + filename)

def build_variance_dataset(hidden_size, activation, architecture, inference, epochs, lr, 
                            n_samples,  warmup, n_inputs, posterior_samples, test_points,
                            device="cuda", rel_path=TESTS):

    _, test_loader, inp_shape, out_size = \
        data_loaders(dataset_name="half_moons", batch_size=64, n_inputs=test_points, shuffle=True)

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                      epochs, lr, n_samples, warmup, n_inputs))
    
    cols = ["hidden_size", "activation", "architecture", "inference", "epochs", "lr", 
            "n_samples", "warmup", "n_inputs", "posterior_samples", "test_acc", "var"]
    df = pandas.DataFrame(columns=cols)

    row_count = 0
    for init in combinations:

        bnn = MoonsBNN(*init, inp_shape, out_size)
        bnn.load(device=device, rel_path=rel_path)
        
        for p_samp in posterior_samples:
            bnn_dict = {cols[k]:val for k, val in enumerate(init)}
            test_acc = bnn.evaluate(test_loader=test_loader, device=device, n_samples=p_samp)
            loss_grads = load_loss_gradients(n_samples=p_samp, filename=bnn.name, 
                                             savedir=bnn.name+"/", relpath=rel_path) 
            loss_grads_var = loss_grads[:test_points].var(0)

            for value in loss_grads_var:
                bnn_dict.update({"posterior_samples":p_samp, "test_acc":test_acc, "var":value})
                df.loc[row_count] = pandas.Series(bnn_dict)
                row_count += 1

    print("\nSaving:", df.head())

    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    df.to_csv(TESTS+"halfMoons_lossGrads_compVariance_"+str(test_points)+".csv", 
              index = False, header=True)
    return df

def scatterplot_gridSearch_variance(dataset, test_points, device="cuda"):

    dataset = dataset[dataset["test_acc"]>ACC_THS]
    print("\n---scatterplot_gridSearch_variance---\n", dataset)

    # categorical = dataset["hidden_size"]
    # ncols = len(np.unique(categorical))

    sns.set_style("darkgrid")
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("", ["orangered","darkred","black"])
    matplotlib.rc('font', **{'weight': 'bold', 'size': 10})
    fig, ax = plt.subplots(nrows=1, ncols=1, figsize=(16, 5), dpi=150, 
                           facecolor='w', edgecolor='k')

    # for idx, val in enumerate(np.unique(categorical)):

    # df = dataset[categorical==val]
    var_ths = np.max(df["var"])*0.01
    df = dataset[dataset["var"]>var_ths]
    g = sns.stripplot(data=df, x="hidden_size", y="var", hue="posterior_samples",
                        # size="posterior_samples", hue="posterior_samples", alpha=0.8, 
                        # sizes=(20, 100), #style="posterior_samples", 
                        ax=ax, palette="gist_heat")

    g.set(ylim=(0, None))
    # ax[idx].set_title(str(val)+" samples")
    ax.set_xlabel(f"{val}")
    ax.set_ylabel("")
    ax.xaxis.set_label_position("top")

    fig.text(0.5, 0.01, 'Posterior samples', ha='center', fontsize=12)
    # fig.text(0.03, 0.5, 'Expected loss gradients components', va='center',fontsize=12,
    # ax.legend(loc='upper right', title="Hidden size")

    # fig.text(0.5, 0.01, "Samples involved in the expectations ($w \sim p(w|D)$)", ha='center')

    filename = "halfMoons_lossGrads_compVariance_"+str(test_points)+".png"
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    plt.savefig(TESTS + filename)


##########################
# robustness vs accuracy #
##########################


def _compute_attacks(method, hidden_size, activation, architecture, inference, 
           epochs, lr, n_samples, warmup, n_inputs, posterior_samples, rel_path, test_points):

    _, _, x_test, y_test, inp_shape, out_size = \
            load_dataset(dataset_name="half_moons", n_inputs=test_points, channels="first") 

    x_test = torch.from_numpy(x_test)
    y_test = torch.from_numpy(y_test)

    bnn = MoonsBNN(hidden_size, activation, architecture, inference, 
                   epochs, lr, n_samples, warmup, n_inputs, inp_shape, out_size)
    bnn.load(device="cpu", rel_path=rel_path)
        
    x_attack = attack(net=bnn, x_test=x_test, y_test=y_test, dataset_name="half_moons", 
                      device="cpu", method=method, filename=bnn.name, 
                      n_samples=posterior_samples)

def parallel_grid_attack(method, hidden_size, activation, architecture, inference, epochs, lr, 
                         n_samples, warmup, n_inputs, posterior_samples, rel_path, test_points):
    from joblib import Parallel, delayed

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                        epochs, lr, n_samples, warmup, n_inputs, posterior_samples))

    Parallel(n_jobs=10)(
        delayed(_compute_attacks)(method, *init, rel_path, test_points) 
                for init in combinations)

def grid_attack(method, hidden_size, activation, architecture, inference, epochs, lr, 
                  n_samples, warmup, n_inputs, posterior_samples, test_points, device="cuda", 
                  rel_path=TESTS):
   
    _, _, x_test, y_test, inp_shape, out_size = \
        load_dataset(dataset_name="half_moons", n_inputs=test_points, channels="first") 

    x_test = torch.from_numpy(x_test)
    y_test = torch.from_numpy(y_test)

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                        epochs, lr, n_samples, warmup, n_inputs))
    for init in combinations:

        bnn = MoonsBNN(*init, inp_shape, out_size)
        bnn.load(device=device, rel_path=rel_path)
        
        for p_samp in posterior_samples:
            x_attack = attack(net=bnn, x_test=x_test, y_test=y_test, dataset_name="half_moons", 
                              device=device, method=method, filename=bnn.name, 
                              n_samples=p_samp)

def build_attack_dataset(method, hidden_size, activation, architecture, inference, epochs, lr, 
                          n_samples, warmup, n_inputs, posterior_samples, 
                         test_points, device="cuda", rel_path=TESTS):
    
    _, _, x_test, y_test, inp_shape, out_size = \
        load_dataset(dataset_name="half_moons", n_inputs=test_points, channels="first") 

    x_test = torch.from_numpy(x_test)
    y_test = torch.from_numpy(y_test)

    combinations = list(itertools.product(hidden_size, activation, architecture, inference, 
                        epochs, lr, n_samples, warmup, n_inputs))

    cols = ["hidden_size", "activation", "architecture", "inference", "epochs", "lr", 
            "n_samples", "warmup", "n_inputs", "posterior_samples", 
            "x_orig","y_orig","x_adv","y_adv","label",
            "test_acc",  "adversarial_acc", "softmax_rob"]
    df = pandas.DataFrame(columns=cols)

    row_count = 0
    for init in combinations:

        bnn = MoonsBNN(*init, inp_shape, out_size)
        bnn.load(device=device, rel_path=DATA)
        
        for p_samp in posterior_samples:
            bnn_dict = {cols[k]:val for k, val in enumerate(init)}

            x_test = x_test[:test_points]
            x_attack = load_attack(model=bnn, method=method, filename=bnn.name, 
                                  n_samples=p_samp, rel_path=rel_path)[:test_points]

            test_acc, adversarial_acc, softmax_rob = \
                   attack_evaluation(model=bnn, x_test=x_test, x_attack=x_attack, y_test=y_test, 
                                     device=device, n_samples=p_samp)
            labels = onehot_to_labels(y_test)

            for idx in range(len(x_test)):
                x_orig, y_orig = x_test[idx].flatten()
                x_adv, y_adv = x_attack[idx].flatten()

                bnn_dict.update({"posterior_samples":p_samp, "test_acc":test_acc, 
                                 "x_orig":x_orig.item(),"y_orig":y_orig.item(),
                                 "label":labels[idx].item(),
                                 "x_adv":x_adv.item(),"y_adv":y_adv.item(),
                                 "adversarial_acc":adversarial_acc, "softmax_rob":softmax_rob})
            
                df.loc[row_count] = pandas.Series(bnn_dict)
                row_count += 1

    print("\nSaving:", df)
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    df.to_csv(TESTS+"halfMoons_attack_"+str(test_points)+"_"+str(method)+".csv", 
              index = False, header=True)
    return df

def plot_rob_acc(dataset, test_points, method, device="cuda"):

    dataset = dataset[dataset["test_acc"]>ACC_THS]
    print("\n---plot_rob_acc---\n", dataset)

    categorical_rows=dataset["hidden_size"]
    nrows = len(np.unique(categorical_rows))
    sns.set_style("darkgrid")
    matplotlib.rc('font', **{'size': 10})
    fig, ax = plt.subplots(nrows=nrows, ncols=1, figsize=(10, 6), dpi=150, facecolor='w', edgecolor='k')

    # cmap = matplotlib.colors.LinearSegmentedColormap.from_list("", ["lightseagreen","darkred","black"])
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("", ["orangered","darkred","black"])

    for i, categ_row in enumerate(np.unique(categorical_rows)):
        df = dataset[categorical_rows==categ_row]
        g = sns.scatterplot(data=df, x="test_acc", y="softmax_rob", 
                            size="n_inputs", hue="n_inputs", alpha=0.9, 
                            ax=ax[i], legend="full", sizes=(20, 100), palette=cmap)
        # ax[i].set_xlim(ACC_THS,101)
        ax[i].set_ylim(-0.1,1.1)
        ax[i].set_xlabel("")
        ax[i].set_ylabel(f"{categ_row}", rotation=270, labelpad=10,
                          fontdict=dict(weight='bold'))
        ax[i].yaxis.set_label_position("right")

        if i != 2:
            ax[i].set(xticklabels=[])
            ax[i].legend_.remove()

    fig.text(0.5, 0.02, r"Test accuracy", fontsize=11, ha='center',fontdict=dict(weight='bold'))
    fig.text(0.06, 0.5, "Softmax robustness", va='center',fontsize=11, rotation='vertical',
        fontdict=dict(weight='bold'))
    fig.text(0.92, 0.5, "Hidden size", va='center',fontsize=10, rotation=270,
        fontdict=dict(weight='bold'))

    filename = "halfMoons_acc_vs_rob_scatter_"+str(test_points)+"_"+str(method)+".png"
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    plt.savefig(TESTS + filename)


def stripplot_rob_acc(dataset, test_points, method, device="cuda"):

    dataset = dataset[dataset["test_acc"]>ACC_THS]
    print("\n---stripplot_rob_acc---\n", dataset)

    sns.set_style("darkgrid")
    matplotlib.rc('font', **{'weight':'bold','size': 10})
    fig, ax = plt.subplots(nrows=2, ncols=2, figsize=(10, 6), dpi=150, facecolor='w', edgecolor='k')

    # cmap = matplotlib.colors.LinearSegmentedColormap.from_list("", ["lightseagreen","darkred","black"])
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("", ["orangered","darkred","black"])

    for idx, y in enumerate(["posterior_samples","hidden_size"]):
        df = dataset[["test_acc", y, "n_inputs", "softmax_rob"]].drop_duplicates()

        g = sns.stripplot(data=df, y=y, x="test_acc", 
                            hue="n_inputs", alpha=0.8,
                            ax=ax[idx,0], palette="gist_heat", orient="h")
        ax[idx,0].set_ylabel("")
        ax[idx,0].set_xlabel("Test accuracy", fontdict=dict(weight='bold'))

        g = sns.stripplot(data=df, y=y, x="softmax_rob",  
                          hue="n_inputs", alpha=0.8, 
                            ax=ax[idx,1], palette="gist_heat", orient="h")
        ax[idx,1].set_ylabel("")
        ax[idx,1].set_xlabel("Softmax robustness", fontdict=dict(weight='bold'))

    ax[0,1].legend_.remove()
    ax[1,1].legend_.remove()
    ax[1,0].legend_.remove()

    # fig.text(0.5, 0.01, r"Test accuracy", fontsize=12, ha='center',fontdict=dict(weight='bold'))
    fig.text(0.03, 0.3, "Hidden size", va='center',fontsize=10, rotation='vertical', fontdict=dict(weight='bold'))
    fig.text(0.03, 0.8, "Posterior samples", va='center',fontsize=10, rotation='vertical', fontdict=dict(weight='bold'))

    filename = "halfMoons_acc_vs_rob_strip_"+str(test_points)+"_"+str(method)+".png"
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    plt.savefig(TESTS + filename)

def plot_attacks(dataset, test_points, method, device="cuda"):
    dataset = dataset[dataset["test_acc"]>ACC_THS].sample(500)
    print("\n---plot_attacks---\n", dataset)

    dataset = pandas.DataFrame(dataset, columns=dataset.columns)
    dataset['color']=dataset["x_orig"]+dataset["y_orig"]

    categorical_cols = dataset["hidden_size"]
    ncols = len(np.unique(categorical_cols))

    sns.set_style("darkgrid")
    cmap1 = matplotlib.colors.LinearSegmentedColormap.from_list("", ["lightseagreen","darkgreen","black"])
    cmap2 = matplotlib.colors.LinearSegmentedColormap.from_list("", ["orangered","darkred","black"])
    cmap = [cmap1, cmap2]
    marker = ["d","o"]
    matplotlib.rc('font', **{'size': 10})
    fig, ax = plt.subplots(nrows=2, ncols=ncols, figsize=(10, 6), dpi=150, 
                           facecolor='w', edgecolor='k')

    vmin, vmax = (dataset["color"].min(), dataset["color"].max())

    for label in [0,1]:
        for c, col_val in enumerate(np.unique(categorical_cols)):
            for r, (x,y) in enumerate([("x_orig","y_orig"),("x_adv","y_adv")]):

                df = dataset[(dataset["label"]==label)&(categorical_cols==col_val)]
                g = sns.scatterplot(data=df, x=x, y=y, alpha=0.7, marker=marker[label],
                                    hue="color",  palette=cmap[label], size="softmax_rob", 
                                    sizes=(20,100), 
                                    ax=ax[r,c], legend=False)
                ax[r,c].set_xlabel("")
                ax[r,c].set_ylabel("")

            ax[-1,c].set_xlabel(str(col_val),labelpad=3,fontdict=dict(weight='bold'))

    ax[0,0].set_ylabel("Original points",labelpad=3,fontdict=dict(weight='bold'))
    ax[1,0].set_ylabel("Adversarial points",labelpad=10,fontdict=dict(weight='bold')) 

    ## colorbar    
    # cbar_ax1 = fig.add_axes([0.93, 0.5, 0.01, 0.35])
    # cbar1 = fig.colorbar(matplotlib.cm.ScalarMappable(norm=None, cmap=cmap1), cax=cbar_ax1)
    # cbar_ax2 = fig.add_axes([0.93, 0.1, 0.01, 0.35])
    # cbar2 = fig.colorbar(matplotlib.cm.ScalarMappable(norm=None, cmap=cmap2), cax=cbar_ax2)

    ## titles and labels
    # fig.text(0.03, 0.5, "Softmax robustness", va='center',fontsize=12, rotation='vertical',
    #     fontdict=dict(weight='bold'))
    fig.text(0.5, 0.01, r"Hidden size", 
        fontsize=12, ha='center',fontdict=dict(weight='bold'))
    fig.suptitle(f"{method} adversarial attack on Half Moons dataset",
               fontsize=12, ha='center', fontdict=dict(weight='bold'))

    filename = "halfMoons_attack_"+str(test_points)+"_"+str(method)+".png"
    os.makedirs(os.path.dirname(TESTS), exist_ok=True)
    plt.savefig(TESTS + filename)


def main(args):


    # === train ===

    # plot_half_moons()

    # posterior_samples = 250
    # _train(args.hidden_size, args.activation, args.architecture, args.inference, 
    #        args.epochs, args.lr, args.samples, args.warmup, args.inputs, posterior_samples)

    # # bnn.load(device=args.device, rel_path=DATA)

    # bnn.evaluate(test_loader=test_loader, device=args.device)

    # === grid search ===
    attack = "fgsm"

    hidden_size = [256]#, 128, 256] 
    activation = ["leaky"]
    architecture = ["fc2"]
    # inference, epochs, lr, n_samples, warmup = (["svi"],[5, 10, 20],[0.01, 0.001, 0.0001],[None],[None])
    inference, epochs, lr, n_samples, warmup =(["hmc"],[None],[None],[250],[50,100])
    n_inputs = [5000, 10000, 15000]
    posterior_samples = [250]
    init = (hidden_size, activation, architecture, inference, 
            epochs, lr, n_samples, warmup, n_inputs, posterior_samples)

    # init = [[arg] for arg in [args.hidden_size, args.activation, args.architecture, 
    #         args.inference, args.epochs, args.lr, args.samples, args.warmup, args.inputs, 3]]

    parallel_train(*init)
    parallel_compute_grads(*init, rel_path=TESTS, test_points=100)
    # parallel_grid_attack(attack, *init, rel_path=TESTS, test_points=100) 
    ## grid_attack(attack, *init, test_points=100, device="cuda", rel_path=DATA) 
    exit()

    # === plots ===
    test_points = 100

    # dataset = build_components_dataset(*init, device=args.device, test_points=test_points, rel_path=TESTS)
    dataset = pandas.read_csv(TESTS+"halfMoons_lossGrads_gridSearch_"+str(test_points)+".csv")
    scatterplot_gridSearch_samp_vs_hidden(dataset=dataset, device=args.device, 
         test_points=100, posterior_samples=[50,100,250], hidden_size=hidden_size)

    # dataset = build_variance_dataset(*init, device=args.device, test_points=test_points, rel_path=DATA)
    # dataset = pandas.read_csv(TESTS+"halfMoons_lossGrads_compVariance_"+str(test_points)+".csv")
    # scatterplot_gridSearch_variance(dataset, test_points, device="cuda")

    # # dataset = build_attack_dataset(attack, *init, device=args.device, test_points=test_points, rel_path=TESTS) 
    # dataset = pandas.read_csv(TESTS+"halfMoons_attack_"+str(test_points)+"_"+str(attack)+".csv")
    # plot_attacks(dataset, test_points, attack, device=args.device)
    # plot_rob_acc(dataset, test_points, attack, device=args.device)
    # stripplot_rob_acc(dataset, test_points, attack, device=args.device)


if __name__ == "__main__":
    assert pyro.__version__.startswith('1.3.0')
    parser = argparse.ArgumentParser(description="Toy example on half moons")

    parser.add_argument("--inputs", default=1000, type=int)
    parser.add_argument("--hidden_size", default=128, type=int, help="power of 2 >= 16")
    parser.add_argument("--activation", default="leaky", type=str, 
                        help="relu, leaky, sigm, tanh")
    parser.add_argument("--architecture", default="fc2", type=str, help="fc, fc2")
    parser.add_argument("--inference", default="svi", type=str, help="svi, hmc")
    parser.add_argument("--epochs", default=10, type=int)
    parser.add_argument("--samples", default=10, type=int)
    parser.add_argument("--warmup", default=5, type=int)
    parser.add_argument("--lr", default=0.001, type=float)
    parser.add_argument("--device", default='cuda', type=str, help="cpu, cuda")  
   
    main(args=parser.parse_args())