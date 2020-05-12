'''
Train a VAE for heterogeneous reconstruction with known pose
'''
import numpy as np
import sys, os
import argparse
import pickle
from datetime import datetime as dt

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from cryodrgn import mrc
from cryodrgn import utils
from cryodrgn import fft
from cryodrgn import lie_tools
from cryodrgn import dataset
from cryodrgn import ctf

from cryodrgn.pose import PoseTracker
from cryodrgn.models import HetOnlyVAE
from cryodrgn.lattice import Lattice
from cryodrgn.beta_schedule import get_beta_schedule, LinearSchedule

log = utils.log
vlog = utils.vlog

def add_args(parser):
    parser.add_argument('particles', type=os.path.abspath, help='Input particles (.mrcs, .star, .cs, or .txt)')
    parser.add_argument('-o', '--outdir', type=os.path.abspath, required=True, help='Output directory to save model')
    parser.add_argument('--zdim', type=int, required=True, help='Dimension of latent variable')
    parser.add_argument('--poses', type=os.path.abspath, required=True, help='Image poses (.pkl)')
    parser.add_argument('--ctf', metavar='pkl', type=os.path.abspath, help='CTF parameters (.pkl)')
    parser.add_argument('--load', metavar='WEIGHTS.PKL', help='Initialize training from a checkpoint')
    parser.add_argument('--checkpoint', type=int, default=1, help='Checkpointing interval in N_EPOCHS (default: %(default)s)')
    parser.add_argument('--log-interval', type=int, default=1000, help='Logging interval in N_IMGS (default: %(default)s)')
    parser.add_argument('-v','--verbose',action='store_true',help='Increaes verbosity')
    parser.add_argument('--seed', type=int, default=np.random.randint(0,100000), help='Random seed')

    group = parser.add_argument_group('Dataset loading')
    group.add_argument('--invert-data', action='store_true', help='Invert data sign')
    group.add_argument('--window', action='store_true', help='Real space windowing of dataset')
    group.add_argument('--ind', type=os.path.abspath, help='Filter particle stack by these indices')
    group.add_argument('--lazy', action='store_true', help='Lazy loading if full dataset is too large to fit in memory')
    group.add_argument('--datadir', type=os.path.abspath, help='Path prefix to particle stack if loading relative paths from a .star or .cs file')

    group = parser.add_argument_group('Tilt series')
    group.add_argument('--tilt', help='Particles (.mrcs)')
    group.add_argument('--tilt-deg', type=float, default=45, help='X-axis tilt offset in degrees (default: %(default)s)')

    group = parser.add_argument_group('Training parameters')
    group.add_argument('-n', '--num-epochs', type=int, default=20, help='Number of training epochs (default: %(default)s)')
    group.add_argument('-b','--batch-size', type=int, default=8, help='Minibatch size (default: %(default)s)')
    group.add_argument('--wd', type=float, default=0, help='Weight decay in Adam optimizer (default: %(default)s)')
    group.add_argument('--lr', type=float, default=1e-4, help='Learning rate in Adam optimizer (default: %(default)s)')
    group.add_argument('--beta', default=1.0, help='Choice of beta schedule or a constant for KLD weight (default: %(default)s)')
    group.add_argument('--beta-control', type=float, help='KL-Controlled VAE gamma. Beta is KL target. (default: %(default)s)')
    group.add_argument('--norm', type=float, nargs=2, default=None, help='Data normalization as shift, 1/scale (default: mean, std of dataset)')

    group = parser.add_argument_group('Pose SGD')
    group.add_argument('--do-pose-sgd', action='store_true', help='Refine poses with gradient descent')
    group.add_argument('--pretrain', type=int, default=1, help='Number of epochs with fixed poses before pose SGD (default: %(default)s)')
    group.add_argument('--emb-type', choices=('s2s2','quat'), default='quat', help='SO(3) embedding type for pose SGD (default: %(default)s)')
    group.add_argument('--pose-lr', type=float, default=3e-4, help='Learning rate for pose optimizer (default: %(default)s)')

    group = parser.add_argument_group('Encoder Network')
    group.add_argument('--qlayers', type=int, default=3, help='Number of hidden layers (default: %(default)s)')
    group.add_argument('--qdim', type=int, default=256, help='Number of nodes in hidden layers (default: %(default)s)')
    group.add_argument('--encode-mode', default='resid', choices=('conv','resid','mlp','tilt'), help='Type of encoder network (default: %(default)s)')
    group.add_argument('--enc-mask', type=int, help='Circular mask of image for encoder (default: D/2; -1 for no mask)')
    group.add_argument('--use-real', action='store_true', help='Use real space image for encoder (for convolutional encoder)')

    group = parser.add_argument_group('Decoder Network')
    group.add_argument('--players', type=int, default=3, help='Number of hidden layers (default: %(default)s)')
    group.add_argument('--pdim', type=int, default=256, help='Number of nodes in hidden layers (default: %(default)s)')
    group.add_argument('--pe-type', choices=('geom_ft','geom_full','geom_lowf','geom_nohighf','linear_lowf','none'), default='geom_lowf', help='Type of positional encoding (default: %(default)s)')
    group.add_argument('--pe-dim', type=int, help='Num sinusoid features in positional encoding (default: D/2)')
    group.add_argument('--domain', choices=('hartley','fourier'), default='fourier', help='Decoder representation domain (default: %(default)s)')
    return parser

def train_batch(model, lattice, y, yt, rot, trans, optim, beta, beta_control=None, tilt=None, ctf_params=None, yr=None):
    optim.zero_grad()
    model.train()
    if trans is not None:
        y, yt = preprocess_input(y, yt, lattice, trans)
    z_mu, z_logvar, z, y_recon, y_recon_tilt, mask = run_batch(model, lattice, y, yt, rot, tilt, ctf_params, yr)
    loss, gen_loss, kld = loss_function(z_mu, z_logvar, y, yt, y_recon, mask, beta, y_recon_tilt, beta_control)
    loss.backward()
    optim.step()
    return loss.item(), gen_loss.item(), kld.item()

def preprocess_input(y, yt, lattice, trans):
    # center the image
    B = y.size(0)
    D = lattice.D
    y = lattice.translate_ht(y.view(B,-1), trans.unsqueeze(1)).view(B,D,D)
    if yt is not None: yt = lattice.translate_ht(yt.view(B,-1), trans.unsqueeze(1)).view(B,D,D)
    return y, yt

def run_batch(model, lattice, y, yt, rot, tilt=None, ctf_params=None, yr=None):
    use_tilt = yt is not None
    use_ctf = ctf_params is not None
    B = y.size(0)
    D = lattice.D
    if use_ctf:
        freqs = lattice.freqs2d.unsqueeze(0).expand(B,*lattice.freqs2d.shape)/ctf_params[:,0].view(B,1,1)
        c = ctf.compute_ctf(freqs, *torch.split(ctf_params[:,1:], 1, 1)).view(B,D,D)
    
    # encode
    if yr is not None:
        input_ = (yr,)
    else:
        input_ = (y,yt) if yt is not None else (y,)
        if use_ctf: input_ = (x*c.sign() for x in input_) # phase flip by the ctf
    z_mu, z_logvar = model.encode(*input_)
    z = model.reparameterize(z_mu, z_logvar)

    # decode 
    mask = lattice.get_circular_mask(D//2) # restrict to circular mask
    y_recon = model.decode(lattice.coords[mask]/lattice.extent/2 @ rot, z).view(B,-1)
    if use_ctf: y_recon *= c.view(B,-1)[:,mask]

    # decode the tilt series
    if use_tilt:
        y_recon_tilt = model.decode(lattice.coords[mask]/lattice.extent/2 @ tilt @ rot, z)
        if use_ctf: y_recon_tilt *= c.view(B,-1)[:,mask]
    else:
        y_recon_tilt = None
    return z_mu, z_logvar, z, y_recon, y_recon_tilt, mask

def loss_function(z_mu, z_logvar, y, yt, y_recon, mask, beta, y_recon_tilt=None, beta_control=None):
    # reconstruction error
    use_tilt = yt is not None
    B = y.size(0)
    gen_loss = F.mse_loss(y_recon, y.view(B,-1)[:, mask])
    if use_tilt:
        gen_loss = .5*gen_loss + .5*F.mse_loss(y_recon_tilt, yt.view(B,-1)[:,mask])
    # latent loss
    kld = -0.5 * torch.mean(1 + z_logvar - z_mu.pow(2) - z_logvar.exp())
    # total loss
    if beta_control is None:
        loss = gen_loss + beta*kld/mask.sum().float()
    else:
        loss = gen_loss + args.beta_control*(beta-kld)**2/mask.sum().float()
    return loss, gen_loss, kld

def eval_z(model, lattice, data, batch_size, device, trans=None, use_tilt=False, ctf_params=None, use_real=False):
    assert not model.training
    z_mu_all = []
    z_logvar_all = []
    data_generator = DataLoader(data, batch_size=batch_size, shuffle=False)
    for minibatch in data_generator:
        ind = minibatch[-1]
        y = minibatch[0].to(device)
        yt = minibatch[1].to(device) if use_tilt else None
        B = len(ind)
        D = lattice.D
        if ctf_params is not None:
            freqs = lattice.freqs2d.unsqueeze(0).expand(B,*lattice.freqs2d.shape)/ctf_params[ind,0].view(B,1,1)
            c = ctf.compute_ctf(freqs, *torch.split(ctf_params[ind,1:], 1, 1)).view(B,D,D)
        if trans is not None:
            y = lattice.translate_ht(y.view(B,-1), trans[ind].unsqueeze(1)).view(B,D,D)
            if yt is not None: yt = lattice.translate_ht(yt.view(B,-1), trans[ind].unsqueeze(1)).view(B,D,D)
        if use_real:
            input_ = (torch.from_numpy(data.particles_real[ind]).to(device),)
        else:
            input_ = (y,yt) if yt is not None else (y,)
        if ctf_params is not None: 
            assert not use_real, "Not implemented"
            input_ = (x*c.sign() for x in input_) # phase flip by the ctf
        z_mu, z_logvar = model.encode(*input_)
        z_mu_all.append(z_mu.detach().cpu().numpy())
        z_logvar_all.append(z_logvar.detach().cpu().numpy())
    z_mu_all = np.vstack(z_mu_all)
    z_logvar_all = np.vstack(z_logvar_all)
    return z_mu_all, z_logvar_all
    
def save_checkpoint(model, optim, epoch, z_mu, z_logvar, out_weights, out_z):
    '''Save model weights, latent encoding z, and decoder volumes'''
    # save model weights
    torch.save({
        'epoch':epoch,
        'model_state_dict':model.state_dict(),
        'optimizer_state_dict':optim.state_dict(),
        }, out_weights)
    # save z
    with open(out_z,'wb') as f:
        pickle.dump(z_mu, f)
        pickle.dump(z_logvar, f)

def save_config(args, dataset, lattice, model, out_config):
    dataset_args = dict(particles=args.particles,
                        norm=dataset.norm,
                        invert_data=args.invert_data,
                        ind=args.ind,
                        keepreal=args.use_real,
                        window=args.window,
                        datadir=args.datadir,
                        ctf=args.ctf,
                        poses=args.poses,
                        do_pose_sgd=args.do_pose_sgd)
    if args.tilt is not None:
        dataset_args['particles_tilt'] = args.tilt
    lattice_args = dict(D=lattice.D,
                        extent=lattice.extent,
                        ignore_DC=lattice.ignore_DC)
    model_args = dict(qlayers=args.qlayers,
                      qdim=args.qdim,
                      players=args.players,
                      pdim=args.pdim,
                      zdim=args.zdim,
                      encode_mode=args.encode_mode,
                      enc_mask=args.enc_mask,
                      pe_type=args.pe_type,
                      pe_dim=args.pe_dim,
                      domain=args.domain)
    config = dict(dataset_args=dataset_args,
                  lattice_args=lattice_args,
                  model_args=model_args)
    config['seed'] = args.seed
    with open(out_config,'wb') as f:
        pickle.dump(config, f)

def get_latest(args):
    # Assumes checkpoint==1, todo: make this more robust
    log('Detecting latest checkpoint...') 
    for i in range(args.num_epochs):
        weights = f'{args.outdir}/weights.{i}.pkl'
        if not os.path.exists(weights):
            break
    args.load =  f'{args.outdir}/weights.{i-1}.pkl'
    log(f'Loading {args.load}')
    if args.do_pose_sgd:
        args.poses = f'{args.outdir}/pose.{i-1}.pkl'
        assert os.path.exists(args.poses)
        log(f'Loading {args.poses}')
    return args

def main(args):
    t1 = dt.now()
    if args.outdir is not None and not os.path.exists(args.outdir):
        os.makedirs(args.outdir)
    LOG = f'{args.outdir}/run.log'
    def flog(msg): # HACK: switch to logging module
        return utils.flog(msg, LOG)
    if args.load == 'latest':
        args = get_latest(args)
    flog(' '.join(sys.argv))
    flog(args)

    # set the random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # set the device
    use_cuda = torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')
    flog('Use cuda {}'.format(use_cuda))
    if use_cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

    # set beta schedule
    try:
        args.beta = float(args.beta)
    except ValueError: 
        assert args.beta_control, "Need to set beta control weight for schedule {}".format(args.beta)
    beta_schedule = get_beta_schedule(args.beta)

    # load the particles
    if args.ind is not None: 
        flog('Filtering image dataset with {}'.format(args.ind))
        ind = pickle.load(open(args.ind,'rb'))
    else: ind = None
    if args.tilt is None:
        if args.encode_mode == 'conv':
            args.use_real = True
        if args.lazy:
            data = dataset.LazyMRCData(args.particles, norm=args.norm, invert_data=args.invert_data, ind=ind, keepreal=args.use_real, window=args.window, datadir=args.datadir)
        else:
            data = dataset.MRCData(args.particles, norm=args.norm, invert_data=args.invert_data, ind=ind, keepreal=args.use_real, window=args.window, datadir=args.datadir)
        tilt = None
    else:
        assert args.encode_mode == 'tilt'
        if args.lazy: raise NotImplementedError
        data = dataset.TiltMRCData(args.particles, args.tilt, norm=args.norm, invert_data=args.invert_data, ind=ind, window=args.window, keepreal=args.use_real, datadir=args.datadir)
        tilt = torch.tensor(utils.xrot(args.tilt_deg).astype(np.float32))
    Nimg = data.N
    D = data.D

    if args.encode_mode == 'conv':
        assert D-1 == 64, "Image size must be 64x64 for convolutional encoder"

    # load poses
    if args.do_pose_sgd: assert args.domain == 'hartley', "Need to use --domain hartley if doing pose SGD"
    do_pose_sgd = args.do_pose_sgd
    posetracker = PoseTracker.load(args.poses, Nimg, D, 's2s2' if do_pose_sgd else None, ind)
    pose_optimizer = torch.optim.SparseAdam(posetracker.parameters(), lr=args.pose_lr) if do_pose_sgd else None

    # load ctf
    if args.ctf is not None:
        if args.use_real:
            raise NotImplementedError("Not implemented with real-space encoder. Use phase-flipped images instead")
        flog('Loading ctf params from {}'.format(args.ctf))
        ctf_params = ctf.load_ctf_for_training(D-1, args.ctf)
        if args.ind is not None: ctf_params = ctf_params[ind]
        ctf_params = torch.tensor(ctf_params)
    else: ctf_params = None

    # instantiate model
    lattice = Lattice(D, extent=0.5)
    if args.enc_mask is None:
        args.enc_mask = D//2
    if args.enc_mask > 0:
        assert args.enc_mask <= D//2
        enc_mask = lattice.get_circular_mask(args.enc_mask)
        in_dim = enc_mask.sum()
    elif args.enc_mask == -1:
        enc_mask = None
        in_dim = lattice.D**2 if not args.use_real else (lattice.D-1)**2
    else: 
        raise RuntimeError("Invalid argument for encoder mask radius {}".format(args.enc_mask))
    model = HetOnlyVAE(lattice, args.qlayers, args.qdim, args.players, args.pdim,
                in_dim, args.zdim, encode_mode=args.encode_mode, enc_mask=enc_mask,
                enc_type=args.pe_type, enc_dim=args.pe_dim, domain=args.domain)
    flog(model)
    flog('{} parameters in model'.format(sum(p.numel() for p in model.parameters() if p.requires_grad)))

    # save configuration
    out_config = '{}/config.pkl'.format(args.outdir)
    save_config(args, data, lattice, model, out_config)
    
    optim = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

    # restart from checkpoint
    if args.load:
        flog('Loading checkpoint from {}'.format(args.load))
        checkpoint = torch.load(args.load)
        model.load_state_dict(checkpoint['model_state_dict'])
        optim.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']+1
        model.train()
    else:
        start_epoch = 0

    # training loop
    data_generator = DataLoader(data, batch_size=args.batch_size, shuffle=True)
    num_epochs = args.num_epochs
    for epoch in range(start_epoch, num_epochs):
        t2 = dt.now()
        gen_loss_accum = 0
        loss_accum = 0
        kld_accum = 0
        eq_loss_accum = 0
        batch_it = 0 
        for minibatch in data_generator:
            ind = minibatch[-1].to(device)
            y = minibatch[0].to(device)
            yt = minibatch[1].to(device) if tilt is not None else None
            B = len(ind)
            batch_it += B
            global_it = Nimg*epoch + batch_it

            beta = beta_schedule(global_it)
           
            yr = torch.from_numpy(data.particles_real[ind.numpy()]).to(device) if args.use_real else None
            if do_pose_sgd:
                pose_optimizer.zero_grad()
            rot, tran = posetracker.get_pose(ind)
            ctf_param = ctf_params[ind] if ctf_params is not None else None
            loss, gen_loss, kld = train_batch(model, lattice, y, yt, rot, tran, optim, beta, args.beta_control, tilt, ctf_params=ctf_param, yr=yr)
            if do_pose_sgd and epoch >= args.pretrain:
                pose_optimizer.step()

            # logging
            gen_loss_accum += gen_loss*B
            kld_accum += kld*B
            loss_accum += loss*B

            if batch_it % args.log_interval == 0:
                log('# [Train Epoch: {}/{}] [{}/{} images] gen loss={:.6f}, kld={:.6f}, beta={:.6f}, loss={:.6f}'.format(epoch+1, num_epochs, batch_it, Nimg, gen_loss, kld, beta, loss))
        flog('# =====> Epoch: {} Average gen loss = {:.6}, KLD = {:.6f}, total loss = {:.6f}; Finished in {}'.format(epoch+1, gen_loss_accum/Nimg, kld_accum/Nimg, loss_accum/Nimg, dt.now()-t2))

        if args.checkpoint and epoch % args.checkpoint == 0:
            out_weights = '{}/weights.{}.pkl'.format(args.outdir,epoch)
            out_z = '{}/z.{}.pkl'.format(args.outdir, epoch)
            model.eval()
            with torch.no_grad():
                z_mu, z_logvar = eval_z(model, lattice, data, args.batch_size, device, posetracker.trans, tilt is not None, ctf_params, args.use_real)
                save_checkpoint(model, optim, epoch, z_mu, z_logvar, out_weights, out_z)
            if args.do_pose_sgd and epoch >= args.pretrain:
                out_pose = '{}/pose.{}.pkl'.format(args.outdir, epoch)
                posetracker.save(out_pose)

    # save model weights, latent encoding, and evaluate the model on 3D lattice
    out_weights = '{}/weights.pkl'.format(args.outdir)
    out_z = '{}/z.pkl'.format(args.outdir)
    model.eval()
    with torch.no_grad():
        z_mu, z_logvar = eval_z(model, lattice, data, args.batch_size, device, posetracker.trans, tilt is not None, ctf_params, args.use_real)
        save_checkpoint(model, optim, epoch, z_mu, z_logvar, out_weights, out_z)
    
    if args.do_pose_sgd and epoch >= args.pretrain:
        out_pose = '{}/pose.pkl'.format(args.outdir)
        posetracker.save(out_pose)
    td = dt.now()-t1
    flog('Finsihed in {} ({} per epoch)'.format(td, td/(num_epochs-start_epoch)))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    args = add_args(parser).parse_args()
    utils._verbose = args.verbose
    main(args)
